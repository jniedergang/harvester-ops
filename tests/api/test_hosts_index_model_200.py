"""2.0.0, tâche 8 : logique pure de l'index des hôtes (`hosts-index.js`).

Chargée dans node : `view(hosts, prefs)` filtre (famille, état ou « en
alerte », étiquette, version, dossier ou « non rangé ») et trie (chaque
colonne, dans les deux sens, égalités par nom) ; `facets(hosts)` donne les
valeurs des menus. Une mutation volontaire (tri par état qui ne met plus
les alertes en tête) doit faire échouer la ligne de tri par état."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

RUNNER = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const X = require('./web/static/js/hosts-index.js');
if (input.mutate === 'state-order') X.STATE_ORDER.reverse();
const out = input.cases.map((c) => X.view(c.hosts, c.prefs).map((h) => `${h.kind}/${h.name}`));
console.log(JSON.stringify({ rows: out, facets: input.cases.map((c) => X.facets(c.hosts)) }));
"""


def run(cases, mutate=None):
    try:
        r = subprocess.run(["node", "-e", RUNNER], input=json.dumps({"cases": cases, "mutate": mutate}),
                           capture_output=True, text=True, cwd=str(ROOT), timeout=30)
    except FileNotFoundError:
        pytest.skip("node absent")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def h(name, kind="harvester", state="ok", version=None, labels=None, vms=None, folder=None, fav=False):
    return {"kind": kind, "name": name, "state": state, "version": version, "labels": labels or {},
            "vm_count": vms, "folder": folder, "favorite": fav}


FLEET = [
    h("harv1", version="v1.9.0", vms=17, labels={"site": "cave"}, fav=True),
    h("harvlab2", state="unreachable", version="v1.8.2"),
    h("gpudemo", version="v1.9.0", vms=3, folder="/Lab"),
    h("node2", "kvm", version="12.8.0", vms=15, labels={"site": "cave", "role": "bench"}),
    h("ghost", "kvm", state="error"),
    h("kvm-b", "kvm", state="unknown", vms=0, folder="/Lab"),
]

# (nom, préférences, clés attendues dans l'ordre)
CASES = [
    ("default-by-name", {}, ["kvm/ghost", "harvester/gpudemo", "harvester/harv1", "harvester/harvlab2",
                             "kvm/kvm-b", "kvm/node2"]),
    ("family-harvester", {"kind": "harvester"}, ["harvester/gpudemo", "harvester/harv1", "harvester/harvlab2"]),
    ("family-kvm", {"kind": "kvm"}, ["kvm/ghost", "kvm/kvm-b", "kvm/node2"]),
    ("state-alert", {"state": "alert"}, ["kvm/ghost", "harvester/harvlab2"]),
    ("state-unknown", {"state": "unknown"}, ["kvm/kvm-b"]),
    ("label", {"label": "site=cave"}, ["harvester/harv1", "kvm/node2"]),
    ("label-and-family", {"label": "site=cave", "kind": "kvm"}, ["kvm/node2"]),
    ("version", {"version": "v1.9.0"}, ["harvester/gpudemo", "harvester/harv1"]),
    ("folder", {"folder": "/Lab"}, ["harvester/gpudemo", "kvm/kvm-b"]),
    ("unfiled", {"folder": "-"}, ["kvm/ghost", "harvester/harv1", "harvester/harvlab2", "kvm/node2"]),
    ("no-match", {"kind": "kvm", "version": "v1.9.0"}, []),
    ("sort-state", {"sort": "state"}, ["harvester/harvlab2", "kvm/ghost", "kvm/kvm-b", "harvester/gpudemo",
                                       "harvester/harv1", "kvm/node2"]),
    ("sort-state-desc", {"sort": "state", "dir": -1}, ["harvester/gpudemo", "harvester/harv1", "kvm/node2",
                                                       "kvm/kvm-b", "kvm/ghost", "harvester/harvlab2"]),
    ("sort-vms-desc", {"sort": "vms", "dir": -1}, ["harvester/harv1", "kvm/node2", "harvester/gpudemo",
                                                   "kvm/kvm-b", "kvm/ghost", "harvester/harvlab2"]),
    ("sort-kind", {"sort": "kind"}, ["harvester/gpudemo", "harvester/harv1", "harvester/harvlab2",
                                     "kvm/ghost", "kvm/kvm-b", "kvm/node2"]),
    ("sort-version", {"sort": "version"}, ["kvm/node2", "harvester/harvlab2", "harvester/gpudemo",
                                           "harvester/harv1", "kvm/ghost", "kvm/kvm-b"]),
    ("sort-favorite", {"sort": "favorite"}, ["harvester/harv1", "kvm/ghost", "harvester/gpudemo",
                                             "harvester/harvlab2", "kvm/kvm-b", "kvm/node2"]),
    ("sort-folder", {"sort": "folder"}, ["harvester/gpudemo", "kvm/kvm-b", "kvm/ghost", "harvester/harv1",
                                         "harvester/harvlab2", "kvm/node2"]),
    ("sort-labels", {"sort": "labels"}, ["kvm/node2", "harvester/harv1", "kvm/ghost", "harvester/gpudemo",
                                         "harvester/harvlab2", "kvm/kvm-b"]),
    ("unknown-sort-falls-back-to-name", {"sort": "x"}, ["kvm/ghost", "harvester/gpudemo", "harvester/harv1",
                                                        "harvester/harvlab2", "kvm/kvm-b", "kvm/node2"]),
]


@pytest.mark.parametrize("name,prefs,want", CASES, ids=[c[0] for c in CASES])
def test_view_filters_and_sorts(name, prefs, want):
    out = run([{"hosts": FLEET, "prefs": prefs}])
    assert out["rows"][0] == want


def test_same_name_in_both_families_is_two_rows_ordered_by_family():
    hosts = [h("twin", "kvm"), h("twin")]
    assert run([{"hosts": hosts, "prefs": {}}])["rows"][0] == ["harvester/twin", "kvm/twin"]


def test_facets_list_the_values_present():
    [f] = run([{"hosts": FLEET, "prefs": {}}])["facets"]
    assert f == {"states": ["unreachable", "error", "unknown", "ok"], "labels": ["role=bench", "site=cave"],
                 "versions": ["12.8.0", "v1.8.2", "v1.9.0"], "folders": ["/Lab"]}


def test_a_hundred_hosts_are_all_rendered_in_order():
    hosts = [h(f"h{i:03d}", "kvm" if i % 2 else "harvester", "unreachable" if i in (40, 77) else "ok")
             for i in range(100)]
    out = run([{"hosts": hosts, "prefs": {}}, {"hosts": hosts, "prefs": {"sort": "state"}},
               {"hosts": hosts, "prefs": {"kind": "kvm"}}])["rows"]
    assert len(out[0]) == 100 and out[0][0].endswith("/h000") and out[0][-1].endswith("/h099")
    assert out[1][:2] == ["harvester/h040", "kvm/h077"]
    assert len(out[2]) == 50


def test_mutation_state_order_is_caught():
    """Tri par état inversé (les alertes à la fin) : la ligne sort-state diverge."""
    out = run([{"hosts": FLEET, "prefs": {"sort": "state"}}], mutate="state-order")
    want = dict((c[0], c[2]) for c in CASES)["sort-state"]
    assert out["rows"][0] != want
