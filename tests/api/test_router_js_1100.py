"""Navigation 2.0, étape 1 : les fonctions pures du routeur (`router.js`).

`screenHash` écrit l'adresse de l'écran affiché, `resolve` rend l'écran d'une
adresse : pour chaque onglet de l'interface actuelle, l'aller-retour doit
retomber sur le même onglet (c'est ce qui rend F5 exact).
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

RUNNER = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
global.window = global;
const Routes = require('./web/static/js/routes.js');
const Router = require('./web/static/js/router.js');
const ctx = { harvester: ['c1'], kvm: ['k1'], lastHarvester: 'c1', lastKvm: 'k1' };
const out = input.screens.map((s) => {
  const h = Router.screenHash(s);
  let r = Routes.parse(h);
  if (r.name === 'unknown') r = Routes.fromLegacy(h, ctx);
  return { h, back: Router.resolve(r) };
});
console.log(JSON.stringify(out));
"""

# Tous les onglets de l'interface actuelle (data-tab et groupes routés)
TABS = sorted(set(re.findall(r'<section id="tab-([a-z]+)"',
                             (ROOT / "web/templates/index.html").read_text(encoding="utf-8"))))
KVM_SCREENS = {"kvmhosts", "kvmvms", "kvmstorage", "kvmnetworks", "kvmparams", "kvmdr"}
SECTION_PANE = {"storage": "images", "network": "overlay", "addons": "list", "security": "sshkeys",
                "monlog": "alerts", "advanced": "pci", "vmimport": "ova", "forklift": "waves"}


def screens():
    out = []
    for tab in TABS:
        s = {"tab": tab, "cluster": "c1", "pane": SECTION_PANE.get(tab), "overview": "metrics", "automation": "capi"}
        out.append(s)
        if tab == "overview":
            out += [dict(s, overview="cluster"), dict(s, overview="events")]
        if tab == "automation":
            out += [dict(s, automation="terraform"), dict(s, automation="pxe")]
        # 2.0.0 : écrans d'un hôte KVM (hôte imposé) ; Paramètres : chaque onglet
        if tab in KVM_SCREENS:
            s.update(kvm="k1", kvmTab="system" if tab == "kvmparams" else None)
            if tab == "kvmparams":
                out += [dict(s, kvmTab="labels"), dict(s, kvmTab="conn")]
    return out


def run(screens_):
    try:
        r = subprocess.run(["node", "-e", RUNNER], input=json.dumps({"screens": screens_}),
                           capture_output=True, text=True, cwd=str(ROOT), timeout=30)
    except FileNotFoundError:
        pytest.skip("node unavailable")
    assert r.returncode == 0, r.stderr[:500]
    return json.loads(r.stdout)


def test_the_template_has_the_expected_tabs():
    assert len(TABS) >= 25 and "overview" in TABS and "drlinks" in TABS


@pytest.mark.parametrize("screen", screens(), ids=lambda s: f"{s['tab']}-{s['overview']}-{s['automation']}-{s.get('kvmTab')}")
def test_every_screen_round_trips(screen):
    [got] = run([screen])
    back = got["back"]
    assert back, f"{got['h']} ne rend aucun écran"
    assert back["tab"] == screen["tab"], got["h"]
    if screen["tab"] in SECTION_PANE:
        assert back["pane"] == screen["pane"], got["h"]
    if screen["tab"] == "overview":
        assert back["overview"] == screen["overview"], got["h"]
    if screen["tab"] == "automation":
        assert back["automation"] == screen["automation"], got["h"]
    if screen["tab"] in KVM_SCREENS:
        assert back["kvm"] == "k1" and got["h"].startswith("#kvm/k1"), got["h"]
        if screen["tab"] == "kvmparams":
            assert back["kvmTab"] == screen["kvmTab"], got["h"]
    host_tabs = {"overview", "namespaces", "containers", "clusterdr", "startup", "shutdown", *SECTION_PANE}
    if screen["tab"] in host_tabs:
        assert back["cluster"] == "c1", got["h"]
        assert got["h"].startswith("#harvester/c1"), got["h"]


def test_without_cluster_the_old_token_is_written():
    [got] = run([{"tab": "storage", "cluster": None, "pane": "images"}])
    assert got["h"] == "#storage"


# Nom accepté par la déclaration d'un cluster mais pas par RFC 1123 : F5 et les
# liens de GPU (Add-ons) et de Migrations (vagues) doivent rouvrir le même écran.
LAB = [{"tab": "overview", "cluster": "Lab_1", "pane": None, "overview": "cluster", "automation": "capi"},
       {"tab": "addons", "cluster": "Lab_1", "pane": "list", "overview": "metrics", "automation": "capi"},
       {"tab": "forklift", "cluster": "Lab_1", "pane": "waves", "overview": "metrics", "automation": "capi"},
       {"tab": "storage", "cluster": "HARV1", "pane": "volumes", "overview": "metrics", "automation": "capi"}]


@pytest.mark.parametrize("screen", LAB, ids=lambda s: f"{s['cluster']}-{s['tab']}")
def test_declared_cluster_names_round_trip(screen):
    [got] = run([screen])
    assert got["h"].startswith("#harvester/" + screen["cluster"] + "/"), got["h"]
    back = got["back"]
    assert back and back["tab"] == screen["tab"] and back["cluster"] == screen["cluster"], got
    if screen["pane"]:
        assert back["pane"] == screen["pane"], got
