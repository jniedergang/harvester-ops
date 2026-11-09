"""2.2.0 : Terraform et Cluster API en onglets, côté adresses, modèle et
historique partagé.

* adresses `#automation/capi/<onglet>` et `#automation/terraform/<onglet>`
  (routes.js), onglet par défaut de chaque sous-onglet (router.js) ;
* bandes de l'en-tête : ordre décidé, info-bulle sur chaque onglet, clés de
  traduction dans les cinq langues ;
* chaque verbe d'action Terraform ou Cluster API créé par le serveur a son
  libellé dans l'historique ;
* auto-history.js (dans node, DOM simulé) : filtre par préfixe exact,
  échappement, relecture tant qu'une action tourne ET que l'onglet est
  affiché, arrêt ;
* mutations volontaires : chaque garde est prouvée par une copie cassée.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
WEB = ROOT / "web"
JS = WEB / "static" / "js"
HTML = (WEB / "templates" / "index.html").read_text(encoding="utf-8")
LANGS = ("en", "fr", "it", "es", "de")


def node(script, stdin="", cwd=ROOT):
    try:
        r = subprocess.run(["node", "-e", script], input=stdin, capture_output=True, text=True,
                           cwd=str(cwd), timeout=30)
    except FileNotFoundError:
        pytest.skip("node absent")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# --- adresses ----------------------------------------------------------------------

ROUTES_RUNNER = r"""
global.window = global; global.document = { addEventListener() {} };
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const m = { exports: {} };
new Function('module', 'globalThis', src)(m, globalThis);
global.Routes = m.exports;
const Router = require('./web/static/js/router.js');
const inp = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(inp.map((h) => {
  const r = Routes.parse(h);
  const t = r.name === 'unknown' ? null : Router.resolve(r);
  return [r.name, r.pane || null, t ? t.automation : null, t ? t.autoTab : null,
          r.name === 'unknown' ? null : Routes.format(r)];
})));
"""

ADDRESSES = [
    # sans onglet : le premier de la liste
    ("#automation/capi", ["automation", None, "capi", "clusters", "#automation/capi"]),
    ("#automation/terraform", ["automation", None, "terraform", "declarations", "#automation/terraform"]),
    ("#automation/pxe", ["automation", None, "pxe", "machines", "#automation/pxe"]),
    ("#automation/capi/clusters", ["automation", "clusters", "capi", "clusters", "#automation/capi/clusters"]),
    ("#automation/capi/services", ["automation", "services", "capi", "services", "#automation/capi/services"]),
    ("#automation/capi/history", ["automation", "history", "capi", "history", "#automation/capi/history"]),
    ("#automation/capi/gitops", ["automation", "gitops", "capi", "gitops", "#automation/capi/gitops"]),
    ("#automation/capi/secrets", ["automation", "secrets", "capi", "secrets", "#automation/capi/secrets"]),
    ("#automation/capi/registries", ["automation", "registries", "capi", "registries", "#automation/capi/registries"]),
    ("#automation/capi/artefacts", ["automation", "artefacts", "capi", "artefacts", "#automation/capi/artefacts"]),
    ("#automation/capi/installation",
     ["automation", "installation", "capi", "installation", "#automation/capi/installation"]),
    ("#automation/terraform/declarations",
     ["automation", "declarations", "terraform", "declarations", "#automation/terraform/declarations"]),
    ("#automation/terraform/resources",
     ["automation", "resources", "terraform", "resources", "#automation/terraform/resources"]),
    ("#automation/terraform/history",
     ["automation", "history", "terraform", "history", "#automation/terraform/history"]),
    ("#automation/terraform/installation",
     ["automation", "installation", "terraform", "installation", "#automation/terraform/installation"]),
    # un chemin d'erreur par ligne
    ("#automation/capi/k8s", ["unknown", None, None, None, None]),
    ("#automation/capi/install", ["unknown", None, None, None, None]),
    ("#automation/terraform/live", ["unknown", None, None, None, None]),
    ("#automation/terraform/decls", ["unknown", None, None, None, None]),
    ("#automation/terraform/machines", ["unknown", None, None, None, None]),
    ("#automation/capi/declarations", ["unknown", None, None, None, None]),
    ("#automation/capi/History", ["unknown", None, None, None, None]),
    ("#automation/capi/history/x", ["unknown", None, None, None, None]),
]


def run_routes(src, hashes):
    return node(ROUTES_RUNNER.replace("process.argv[1]", json.dumps(str(src))), json.dumps(hashes))


def test_capi_and_terraform_tab_addresses():
    got = run_routes(JS / "routes.js", [h for h, _ in ADDRESSES])
    assert got == [want for _, want in ADDRESSES]


@pytest.mark.parametrize("old,new,probe", [
    ("capi: ['clusters', 'services', 'gitops', 'secrets', 'registries', 'artefacts', 'history', 'installation']", "capi: []",
     "#automation/capi/services"),
    ("terraform: ['declarations', 'resources', 'history', 'installation']", "terraform: []",
     "#automation/terraform/history"),
])
def test_mutation_without_a_tab_list_the_addresses_are_unknown(tmp_path, old, new, probe):
    """Mutation volontaire : sans la liste d'onglets, l'adresse est refusée."""
    src = (JS / "routes.js").read_text()
    broken = src.replace(old, new)
    assert broken != src
    p = tmp_path / "routes.js"
    p.write_text(broken)
    assert run_routes(p, [probe])[0][0] == "unknown"


@pytest.mark.parametrize("old,new,probe", [
    # l'ordre décide de l'onglet par défaut : Clusters avant Installation
    ("capi: ['clusters', 'services', 'gitops', 'secrets', 'registries', 'artefacts', 'history', 'installation']",
     "capi: ['installation', 'clusters', 'services', 'gitops', 'secrets', 'registries', 'artefacts', 'history']", "#automation/capi"),
    ("terraform: ['declarations', 'resources', 'history', 'installation']",
     "terraform: ['resources', 'declarations', 'history', 'installation']", "#automation/terraform"),
])
def test_mutation_the_order_gives_the_default_tab(tmp_path, old, new, probe):
    src = (JS / "routes.js").read_text()
    p = tmp_path / "routes.js"
    p.write_text(src.replace(old, new))
    want = dict(ADDRESSES)[probe]
    with pytest.raises(AssertionError):
        assert run_routes(p, [probe])[0] == want


# --- bandes de l'en-tête -------------------------------------------------------------

def strip(only_for):
    head = HTML.split(f'data-only-for="{only_for}"', 1)[1].split("</div>", 1)[0]
    return re.findall(r"<button[^>]*>", head)


@pytest.mark.parametrize("only_for,attr,order", [
    ("capi", "data-capi-tab", ["clusters", "services", "gitops", "secrets", "registries", "artefacts", "history", "installation"]),
    ("terraform", "data-tf-tab", ["declarations", "resources", "history", "installation"]),
])
def test_header_strips_have_the_decided_order_and_tooltips(only_for, attr, order):
    buttons = strip(only_for)
    assert [re.search(attr + r'="([a-z]+)"', b).group(1) for b in buttons] == order
    for b in buttons:
        assert 'role="tab"' in b and "aria-selected=" in b and 'type="button"' in b
        assert 'title="' in b and "data-tip-i18n=" in b and "data-i18n-title=" in b
    # le premier (défaut) est actif dans le modèle
    assert "active" in buttons[0] and all("active" not in b for b in buttons[1:])


def translations():
    """Clés des cinq blocs de i18n.js (lecture textuelle, comme test_i18n)."""
    src = (JS / "i18n.js").read_text(encoding="utf-8")
    out = {}
    starts = [(m.start(), m.group(1)) for m in re.finditer(r"^  ([a-z]{2}): \{$", src, re.M)]
    for i, (pos, lang) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(src)
        out[lang] = set(re.findall(r"^\s*['\"]([A-Za-z0-9_.-]+)['\"]\s*:", src[pos:end], re.M))
    return out


KEYS_220 = [
    "capi.tabs", "tf.tabs", "capi.tab.clusters", "capi.tab.clusters.tip", "capi.tab.history",
    "capi.tab.history.tip", "capi.hist.title", "capi.hist.refreshTip", "capi.hist.hint", "capi.hist.empty",
    "capi.banner.missing", "capi.banner.open", "capi.banner.openTip", "tf.ui.history", "tf.ui.t.history",
    "tf.hist.hint", "tf.hist.empty", "tf.banner.provider", "tf.banner.cli", "tf.banner.open", "tf.banner.openTip",
]


def test_new_keys_exist_in_the_five_languages():
    tr = translations()
    assert set(tr) == set(LANGS)
    missing = [(lang, k) for lang in LANGS for k in KEYS_220 if k not in tr[lang]]
    assert missing == []
    # les anciens noms des onglets ne restent pas en clés orphelines
    assert not any("capi.tab.k8s" in tr[lang] for lang in LANGS)


def test_the_shared_history_loads_before_its_users():
    pos = {n: HTML.index(f'/static/js/{n}"') for n in ("auto-history.js", "capi.js", "terraform.js", "bmc.js")}
    assert pos["auto-history.js"] < min(pos["capi.js"], pos["terraform.js"], pos["bmc.js"])


def test_no_remembered_sub_tab_is_left():
    """L'onglet vient de l'adresse : plus de choix mémorisé, plus de seconde
    bande Terraform dans le corps de la carte."""
    js = {p.name: p.read_text(encoding="utf-8") for p in JS.glob("*.js")}
    left = [n for n, src in js.items()
            if "harvester_ops_tf_subtab" in src or "harvester_ops_capi_subtab" in src or "sub-tabs-2nd" in src]
    assert left == []
    css = (WEB / "static" / "css" / "style.css").read_text(encoding="utf-8")
    assert ".sub-tabs-2nd" not in css and ".tf-subtab-content" not in css


# --- libellés de l'historique ----------------------------------------------------------

def server_verbs(prefix_re):
    """Verbes d'action créés par app.py (premier segment avant ':')."""
    src = (WEB / "app.py").read_text(encoding="utf-8")
    found = set(re.findall(r'f?"(' + prefix_re + r')[:"]', src))
    if prefix_re.startswith("tf-"):
        # étiquettes du lanceur des déclarations : {"plan": "tf-plan-decl", ...}
        found |= set(re.findall(r'"(tf-(?:plan|apply|destroy)-decl)"', src))
    return found


@pytest.mark.parametrize("js,prefix_re,expected_min", [
    ("capi.js", r"capi-(?:install|uninstall|cleanup-legacy|bundle-build|cluster-[a-z]+|service-[a-z]+)", 9),
    ("terraform.js", r"tf-(?:plan|apply|destroy)[a-z-]*", 6),
])
def test_every_server_verb_has_its_history_label(js, prefix_re, expected_min):
    verbs = server_verbs(prefix_re)
    assert len(verbs) >= expected_min, verbs
    src = (JS / js).read_text(encoding="utf-8")
    table = src.split("function kindLabel", 1)[1].split("default:", 1)[0]
    assert [v for v in sorted(verbs) if f"case '{v}'" not in table] == []


def test_mutation_a_verb_without_label_is_seen():
    src = (JS / "capi.js").read_text(encoding="utf-8")
    table = src.split("function kindLabel", 1)[1].split("default:", 1)[0].replace("case 'capi-cluster-scale'", "")
    with pytest.raises(AssertionError):
        assert "case 'capi-cluster-scale'" in table


# --- auto-history.js dans node --------------------------------------------------------

HISTORY_RUNNER = r"""
const fs = require('fs');
const inp = JSON.parse(fs.readFileSync(0, 'utf8'));
global.window = global;
const box = { innerHTML: '', dataset: {}, addEventListener: () => {}, contains: () => true };
global.document = { body: { contains: () => true } };
global.i18n = { t: (k) => k };
global.Icons = { svg: () => '' };
const asked = [];
global.fetch = (url) => {
  asked.push(url);
  const q = decodeURIComponent(url.split('action=')[1]);
  if (inp.fail) return Promise.resolve({ ok: false, status: 500, json: async () => ({}) });
  return Promise.resolve({ ok: true, json: async () => inp.answers[q] || { in_progress: [], actions_done: [] } });
};
let timers = [];
global.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
global.clearTimeout = (id) => { if (timers[id - 1]) timers[id - 1].fn = null; };
new Function(fs.readFileSync(process.argv[1], 'utf8'))();
let shown = true;
const h = AutoHistory.create({ box: () => box, queries: inp.queries, cls: 'tf', shown: () => shown,
  cluster: true, label: (a) => 'L:' + a.action.split(':')[0], hint: () => 'HINT', empty: () => 'EMPTY' });
(async () => {
  await h.render();
  const first = box.innerHTML;
  const pending = timers.filter(t => t.fn).length;
  // relecture : le minuteur part, l'onglet encore affiché
  let reread = 0;
  if (pending) { const t = timers.filter(x => x.fn).pop(); const fn = t.fn; t.fn = null; fn(); await new Promise(r => setImmediate(r)); reread = asked.length; }
  // onglet quitté : le minuteur suivant ne relit plus
  shown = false;
  const before = asked.length;
  timers.filter(t => t.fn).forEach(t => { const fn = t.fn; t.fn = null; fn(); });
  await new Promise(r => setImmediate(r));
  const afterLeave = asked.length - before;
  shown = true;
  await h.render();
  h.stop();
  console.log(JSON.stringify({ html: first, pending, asked: asked.length, reread, afterLeave,
                               leftTimers: timers.filter(t => t.fn).length,
                               ids: [...first.matchAll(/data-action-id="([^"]+)"/g)].map(m => m[1]) }));
})();
"""

TF_ANSWERS = {
    "tf-plan": {"in_progress": [{"id": "p1", "action": "tf-plan-decl:web:2", "status": "running",
                                 "started_at": 20, "cluster": "harv1"}],
                "actions_done": [{"id": "x1", "action": "vm:delete:default/tf-plan-x", "status": "done",
                                  "started_at": 5, "ended_at": 6, "cluster": "harv1"}]},
    "tf-apply": {"in_progress": [], "actions_done": [
        {"id": "a1", "action": "tf-apply-decl:web:2", "status": "done", "started_at": 10, "ended_at": 400,
         "cluster": "harv1"},
        {"id": "p1", "action": "tf-plan-decl:web:2", "status": "running", "started_at": 20, "cluster": "harv1"}]},
    "tf-destroy": {"in_progress": [], "actions_done": [
        {"id": "d1", "action": 'tf-destroy-resource:<img src=x onerror="pwn()">', "status": "error",
         "started_at": 1, "ended_at": 2, "cluster": "harv1"}]},
}


def run_history(answers, queries=("tf-plan", "tf-apply", "tf-destroy"), fail=False, src=None):
    script = HISTORY_RUNNER.replace("process.argv[1]", json.dumps(str(src or JS / "auto-history.js")))
    return node(script, json.dumps({"answers": answers, "queries": list(queries), "fail": fail}))


def test_history_keeps_only_the_prefixes_and_deduplicates():
    d = run_history(TF_ANSWERS)
    # en cours d'abord, puis passées par date de fin ; l'action d'une VM
    # nommée tf-plan-x n'entre pas ; p1 vu deux fois, montré une fois
    assert d["ids"] == ["p1", "a1", "d1"]
    assert d["asked"] >= 3
    assert "L:tf-plan-decl" in d["html"] and "harv1" in d["html"]
    assert 'href="#activity/action/a1"' in d["html"]
    assert "7 min" in d["html"]                                    # 390 s


def test_history_escapes_what_it_shows():
    d = run_history(TF_ANSWERS)
    assert "<img" not in d["html"] and "&lt;img src=x" in d["html"]


def test_history_reads_again_only_while_shown():
    d = run_history(TF_ANSWERS)
    assert d["pending"] == 1                       # une action tourne : relecture prévue
    assert d["reread"] >= 6                        # relue (3 requêtes de plus)
    assert d["afterLeave"] == 0                    # onglet quitté : plus rien
    assert d["leftTimers"] == 0                    # stop() coupe le minuteur


def test_history_without_running_action_does_not_poll():
    answers = {k: {"in_progress": [], "actions_done": [a for a in v["actions_done"] if a["status"] != "running"]}
               for k, v in TF_ANSWERS.items()}
    d = run_history(answers)
    assert d["pending"] == 0 and d["ids"] == ["a1", "d1"]


@pytest.mark.parametrize("fail,answers,expected", [
    (True, {}, "bmh.failed"),
    (False, {}, "EMPTY"),
])
def test_history_error_and_empty_paths(fail, answers, expected):
    d = run_history(answers, fail=fail)
    assert expected in d["html"] and d["ids"] == []


def test_mutation_without_the_prefix_check_a_foreign_action_enters(tmp_path):
    src = (JS / "auto-history.js").read_text()
    broken = src.replace("|| !keep(a)", "")
    assert broken != src
    p = tmp_path / "auto-history.js"
    p.write_text(broken)
    with pytest.raises(AssertionError):
        assert "x1" not in run_history(TF_ANSWERS, src=p)["ids"]


def test_mutation_without_the_shown_check_polling_goes_on(tmp_path):
    src = (JS / "auto-history.js").read_text()
    broken = src.replace("if (o.shown()) render();", "render();")
    assert broken != src
    p = tmp_path / "auto-history.js"
    p.write_text(broken)
    with pytest.raises(AssertionError):
        assert run_history(TF_ANSWERS, src=p)["afterLeave"] == 0
