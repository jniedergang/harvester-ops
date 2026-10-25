"""2.0.0, tâche 15 : logique pure du panneau des dossiers (`folders.js`) et
adresses de la vue des VMs et de la vue « Dossiers » (`router.js`).

Chargées dans node : index (frères triés par nom), sous-arbre, `within`
(un préfixe de nom n'est pas un parent), lignes d'un dossier (sous-dossiers
compris) ou non rangées, recherche, cibles d'un vidage (ni le dossier ni
ses descendants), adresses des nœuds ; et pour le routeur, chaque adresse
de dossier ou d'hôte vers son écran et retour (F5 rend l'écran exact). Une
mutation volontaire (`within` par simple préfixe) doit faire échouer les
lignes « /Prod » contre « /Production »."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

RUNNER = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
global.window = global;
const F = require('./web/static/js/folders.js');
const Routes = require('./web/static/js/routes.js');
const Router = require('./web/static/js/router.js');
const within = input.mutate ? ((p, x) => !!p && p.startsWith(x)) : F.within;
const rowsIn = (rows, path) => (path === 'unfiled' ? rows.filter((r) => !r.folder)
  : path == null ? rows : rows.filter((r) => within(r.folder, path)));
const out = {
  rows: input.rows.map((c) => (input.mutate ? rowsIn : F.rowsIn)(c.rows, c.path).map((r) => r.name)),
  subtree: F.subtree(input.tree, 1),
  roots: (F.index(input.tree).children.get('') || []).map((f) => f.name),
  search: F.search(input.tree, 'prod erp').map((f) => f.path),
  targets: F.targets(input.tree, 1),
  hrefs: [F.hrefOf('vms', null), F.hrefOf('vms', 'unfiled'), F.hrefOf('vms', '3'), F.hrefOf('folders', '3')],
  routes: input.addresses.map((h) => {
    const t = Router.resolve(Routes.parse(h));
    return t ? [t.tab, Router.screenHash(t)] : null;
  }),
};
console.log(JSON.stringify(out));
"""

TREE = [{"id": 1, "parent_id": None, "name": "Prod", "path": "/Prod"},
        {"id": 2, "parent_id": 1, "name": "ERP", "path": "/Prod/ERP"},
        {"id": 3, "parent_id": None, "name": "Production", "path": "/Production"},
        {"id": 4, "parent_id": 2, "name": "db", "path": "/Prod/ERP/db"},
        {"id": 5, "parent_id": None, "name": "archive", "path": "/archive"}]
ROWS = [{"name": "a", "folder": "/Prod"}, {"name": "b", "folder": "/Prod/ERP/db"},
        {"name": "c", "folder": "/Production"}, {"name": "d", "folder": None}]
ADDRESSES = ["#vms", "#vms/folder/12", "#vms/folder/unfiled", "#vms/host/harvester", "#vms/host/harvester/harv1",
             "#vms/host/kvm/node2", "#vms/host/kvm", "#folders", "#folders/12", "#folders/unfiled",
             "#containers", "#containers/folder/12", "#hosts/folder/12", "#hosts/kvm"]


def run(mutate=False):
    cases = [{"rows": ROWS, "path": p} for p in ("/Prod", "/Production", "unfiled", None, "/Prod/ERP")]
    try:
        r = subprocess.run(["node", "-e", RUNNER], input=json.dumps(
            {"tree": TREE, "rows": cases, "addresses": ADDRESSES, "mutate": mutate}),
            capture_output=True, text=True, cwd=str(ROOT), timeout=30)
    except FileNotFoundError:
        pytest.skip("node absent")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.parametrize("i,want", [
    (0, ["a", "b"]),                 # /Prod : lui et ses sous-dossiers, pas /Production
    (1, ["c"]),
    (2, ["d"]),                      # non rangées
    (3, ["a", "b", "c", "d"]),       # toutes
    (4, ["b"]),
])
def test_rows_of_a_folder(i, want):
    assert run()["rows"][i] == want


def test_tree_helpers():
    out = run()
    assert sorted(out["subtree"]) == ["1", "2", "4"]
    assert out["roots"] == ["archive", "Prod", "Production"]          # sans casse
    assert out["search"] == ["/Prod/ERP", "/Prod/ERP/db"]
    assert out["targets"] == ["unfiled", "5", "3"]                     # ni /Prod ni ses descendants, triés par chemin
    assert out["hrefs"] == ["#vms", "#vms/folder/unfiled", "#vms/folder/3", "#folders/3"]


@pytest.mark.parametrize("i,tab,back", [
    (0, "allvms", "#vms"), (1, "allvms", "#vms/folder/12"), (2, "allvms", "#vms/folder/unfiled"),
    (3, "allvms", "#vms/host/harvester"), (4, "allvms", "#vms/host/harvester/harv1"),
    (5, "allvms", "#vms/host/kvm/node2"),
    (6, "kvmvms", "#vms/host/kvm"),            # la famille KVM garde la vue des VMs KVM et ses gestes
    (7, "folders", "#folders"), (8, "folders", "#folders/12"), (9, "folders", "#folders/unfiled"),
    (10, "allcontainers", "#containers"), (11, "allcontainers", "#containers/folder/12"),
    (12, "hosts", "#hosts/folder/12"), (13, "hosts", "#hosts/kvm"),
])
def test_each_folder_or_host_address_opens_its_screen_and_comes_back(i, tab, back):
    assert run()["routes"][i] == [tab, back]


def test_folders_ui_mutation_within_by_prefix():
    """Mutation volontaire : un simple préfixe range /Production dans /Prod."""
    assert run(mutate=True)["rows"][0] != ["a", "b"]
