"""Navigation 2.0, étape 1 : grammaire pure des adresses (`web/static/js/routes.js`).

Le module est chargé dans node (comme `test_tf_schema_i18n.py`) ; le test
saute si node est absent. Chaque ligne du tableau des adresses doit faire
l'aller-retour `format(parse(x)) == x`, chaque ancien jeton se traduire, et
chaque chemin d'erreur rendre `unknown` (ou `null` pour un jeton vide).
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

# Exécuté par node : lit {cases, legacy, ctx, mutate} sur stdin, rend les résultats.
RUNNER = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const R = require('./web/static/js/routes.js');
if (input.mutate) delete R.LEGACY[input.mutate];
const out = {
  parsed: input.cases.map((h) => { const r = R.parse(h); return { r, back: R.format(r) }; }),
  legacy: input.legacy.map(([t, ctx]) => R.fromLegacy(t, ctx)),
  formatted: (input.routes || []).map((r) => R.format(r)),
  tables: { LEGACY: R.LEGACY, HARVESTER_PARAMS: R.HARVESTER_PARAMS, KVM_PARAMS: R.KVM_PARAMS,
            HARVESTER_PANES: R.HARVESTER_PANES, IMPORT_PANES: R.IMPORT_PANES },
};
console.log(JSON.stringify(out));
"""


def run_node(cases=(), legacy=(), routes=(), mutate=None):
    payload = json.dumps({"cases": list(cases), "legacy": [list(x) for x in legacy],
                          "routes": list(routes), "mutate": mutate})
    try:
        r = subprocess.run(["node", "-e", RUNNER], input=payload, capture_output=True,
                           text=True, cwd=str(ROOT), timeout=30)
    except FileNotFoundError:
        pytest.skip("node unavailable")
    if r.returncode != 0:
        pytest.fail("node failed: " + r.stderr[:500])
    return json.loads(r.stdout)


HARVESTER_PANES = {
    "nodes": [], "storage": ["volumes", "images", "classes"],
    "network": ["vmnets", "clusternets", "lbs", "pools", "hostnets", "overlay", "underlay"],
    "addons": ["list"], "security": ["secrets", "sshkeys", "accounts"],
    "monlog": ["metrics", "alerts", "flows", "outputs"],
    "advanced": ["settings", "pci", "usb", "sriov", "support"],
    "upgrade": [], "power": ["startup", "shutdown"], "conn": [],
}
IMPORT_PANES = {"vmimport": ["imports", "vmware", "openstack", "ova"],
                "forklift": ["prep", "sources", "inventory", "waves"]}
KVM_PARAMS = ["system", "pools", "networks", "labels", "conn"]


def address_table():
    """Toutes les lignes du tableau des adresses (plan, en-tête), développées."""
    c, h = "harv1", "kvm-paris.01"
    rows = [
        "#vms", "#vms/folder/42", "#vms/folder/unfiled",
        f"#vms/host/harvester/{c}", f"#vms/host/kvm/{h}", "#vms/host/harvester", "#vms/host/kvm",
        "#folders", "#folders/42",
        "#containers", "#containers/folder/42",
        "#hosts", "#hosts/harvester", "#hosts/kvm", "#hosts/folder/42",
        f"#harvester/{c}", f"#harvester/{c}/overview/events",
        f"#harvester/{c}/vms", f"#harvester/{c}/containers",
        f"#harvester/{c}/dr", f"#kvm/{h}/dr",
        f"#kvm/{h}", f"#kvm/{h}/vms",
        "#dr", "#dr/rp-0123456789ab", "#dr/links",
        "#migrations", "#gpus", "#bookings", "#usage",
        "#automation/capi", "#automation/terraform", "#automation/pxe",
        "#activity", f"#activity/host/harvester/{c}", f"#activity/host/kvm/{h}",
        # nom Harvester à point
        "#harvester/lab.home.lo/params/storage/images",
        # règle de déclaration d'un cluster (cluster_decl.NAME_RE) : majuscules et _
        "#harvester/Lab_1/params/nodes", "#harvester/Lab_1/params/addons/list",
        "#harvester/Lab_1/imports/forklift/waves", "#harvester/HARV1/vms",
        "#activity/host/harvester/Lab_1", "#vms/host/harvester/Lab_1",
    ]
    for tab, panes in IMPORT_PANES.items():
        rows.append(f"#harvester/{c}/imports/{tab}")
        rows += [f"#harvester/{c}/imports/{tab}/{p}" for p in panes]
    for tab, panes in HARVESTER_PANES.items():
        rows.append(f"#harvester/{c}/params/{tab}")
        rows += [f"#harvester/{c}/params/{tab}/{p}" for p in panes]
    rows += [f"#kvm/{h}/params/{t}" for t in KVM_PARAMS]
    return rows


ROWS = address_table()


@pytest.fixture(scope="module")
def parsed():
    return dict(zip(ROWS, run_node(cases=ROWS)["parsed"]))


@pytest.mark.parametrize("address", ROWS)
def test_every_address_round_trips(parsed, address):
    got = parsed[address]
    assert got["r"]["name"] != "unknown", f"{address} illisible"
    assert got["back"] == address


# Quelques routes complètes : le contenu, pas seulement l'aller-retour.
EXPECTED = [
    ("#harvester/harv1/params/network/overlay",
     {"name": "host", "kind": "harvester", "host": "harv1", "sub": "params", "tab": "network",
      "pane": "overlay", "id": None}),
    ("#kvm/kvm-paris.01/params/pools",
     {"name": "host", "kind": "kvm", "host": "kvm-paris.01", "sub": "params", "tab": "pools",
      "pane": None, "id": None}),
    ("#harvester/harv1",
     {"name": "host", "kind": "harvester", "host": "harv1", "sub": None, "tab": None,
      "pane": None, "id": None}),
    ("#vms/folder/unfiled",
     {"name": "vms", "kind": None, "host": None, "sub": None, "tab": None, "pane": None,
      "id": "unfiled"}),
    ("#vms/host/kvm",
     {"name": "vms", "kind": "kvm", "host": None, "sub": None, "tab": None, "pane": None,
      "id": None}),
    ("#dr/links",
     {"name": "dr", "kind": None, "host": None, "sub": None, "tab": "links", "pane": None,
      "id": None}),
    ("#dr/rp-0123456789ab",
     {"name": "dr", "kind": None, "host": None, "sub": None, "tab": None, "pane": None,
      "id": "rp-0123456789ab"}),
    ("#activity/host/kvm/kvm-paris.01",
     {"name": "activity", "kind": "kvm", "host": "kvm-paris.01", "sub": None, "tab": None,
      "pane": None, "id": None}),
    # segment encodé sans caractère interdit : décodé puis accepté
    ("#kvm/kvm%2Dparis",
     {"name": "host", "kind": "kvm", "host": "kvm-paris", "sub": None, "tab": None,
      "pane": None, "id": None}),
]


@pytest.mark.parametrize("address,route", EXPECTED, ids=[a for a, _ in EXPECTED])
def test_route_content(address, route):
    got = run_node(cases=[address])["parsed"][0]["r"]
    assert got == route


# Un chemin d'erreur par ligne : chacun rend `unknown` et garde l'adresse brute.
ERRORS = [
    ("#xen/a", "famille inconnue"),
    ("#harvester/a/params/foo", "onglet inconnu"),
    ("#harvester/a/params/storage/zzz", "pane inconnu"),
    ("#harvester/a/params/nodes/x", "pane sur un onglet qui n'en a pas"),
    ("#harvester/-a", "nom invalide (tiret en tête)"),
    ("#harvester/_a", "nom invalide (souligné en tête)"),
    ("#harvester/" + "a" * 62, "nom de cluster trop long"),
    ("#kvm/-a", "nom KVM invalide"),
    ("#kvm/a%2Fb", "barre oblique encodée dans un nom"),
    ("#harvester/a%2Fb/vms", "barre oblique encodée dans un nom de cluster"),
    ("#harvester/%E0%A4%A", "encodage illisible"),
    ("#harvester//vms", "segment vide"),
    ("#vms/", "segment final vide"),
    ("#harvester", "hôte manquant"),
    ("#kvm/h/containers", "conteneurs d'un hôte KVM"),
    ("#kvm/h/params/storage", "onglet Harvester sur un hôte KVM"),
    ("#harvester/a/overview", "aperçu sans bloc"),
    ("#harvester/a/imports/xen", "import inconnu"),
    ("#harvester/a/imports/forklift/zzz", "pane d'import inconnu"),
    ("#harvester/a/vms/extra", "segment en trop"),
    ("#vms/folder/-x", "identifiant invalide"),
    ("#vms/host/xen", "famille inconnue sous vms"),
    ("#hosts/xen", "famille inconnue sous hosts"),
    ("#dr/a/b", "segment en trop sous dr"),
    ("#automation", "automation sans sous-onglet (ancien jeton)"),
    ("#automation/xen", "sous-onglet inconnu"),
    ("#gpus/x", "racine avec segment"),
    ("#activity/host/harvester/-a", "activité, nom invalide"),
    ("#storage", "ancien jeton, pas une adresse"),
    ("", "adresse vide"),
    ("#", "dièse seul"),
]


@pytest.mark.parametrize("address,why", ERRORS, ids=[w for _, w in ERRORS])
def test_error_paths_render_unknown(address, why):
    got = run_node(cases=[address])["parsed"][0]
    assert got["r"] == {"name": "unknown", "raw": address.lstrip("#")}, why


def _js_name_res():
    src = (ROOT / "web/static/js/routes.js").read_text()
    return dict(re.findall(r"const (\w+_NAME_RE) = /(.+?)/;", src))


def test_host_name_rule_is_the_server_declaration_rule():
    """Un cluster se déclare avec cluster_decl.NAME_RE et un hôte KVM avec
    kvm_decl.NAME_RE : la grammaire reprend ces règles telles quelles (une
    règle plus stricte rendait illisibles les adresses de `Lab_1`)."""
    import sys
    sys.path.insert(0, str(ROOT / "bin/lib"))
    import cluster_decl
    import kvm_decl
    found = _js_name_res()
    assert found, "aucune règle de nom dans routes.js"
    for name, pattern in found.items():
        assert pattern in (cluster_decl.NAME_RE.pattern, kvm_decl.NAME_RE.pattern), name
    assert cluster_decl.NAME_RE.pattern in found.values()


def test_cluster_and_kvm_host_of_the_same_name_are_two_routes():
    out = run_node(cases=["#harvester/lab", "#kvm/lab"])["parsed"]
    a, b = out[0]["r"], out[1]["r"]
    assert a["host"] == b["host"] == "lab"
    assert (a["kind"], b["kind"]) == ("harvester", "kvm")
    assert out[0]["back"] != out[1]["back"]


# ---------------------------------------------------------------------------
# Anciens jetons
# ---------------------------------------------------------------------------
CTX = {"lastHarvester": "harv2", "lastKvm": "kvm-b",
       "harvester": ["harv1", "harv2"], "kvm": ["kvm-a", "kvm-b"]}

LEGACY_ROWS = [
    ("#overview", "#harvester/harv2"),
    ("#cluster", "#harvester/harv2"),
    ("#storage", "#harvester/harv2/params/storage"),
    ("#network", "#harvester/harv2/params/network"),
    ("#addons", "#harvester/harv2/params/addons"),
    ("#security", "#harvester/harv2/params/security"),
    ("#monlog", "#harvester/harv2/params/monlog"),
    ("#advanced", "#harvester/harv2/params/advanced"),
    ("#startup", "#harvester/harv2/params/power/startup"),
    ("#shutdown", "#harvester/harv2/params/power/shutdown"),
    ("#namespaces", "#harvester/harv2/vms"),
    ("#allvms", "#vms"),
    ("#containers", "#containers"),
    ("#allcontainers", "#containers"),
    ("#kvmhosts", "#hosts"),
    ("#kvm-hosts", "#hosts"),
    ("#kvmvms", "#vms/host/kvm"),
    ("#kvmstorage", "#kvm/kvm-b/params/pools"),
    ("#kvmnetworks", "#kvm/kvm-b/params/networks"),
    ("#vmimport", "#harvester/harv2/imports/vmimport"),
    ("#forklift", "#harvester/harv2/imports/forklift"),
    ("#vmio", "#harvester/harv2/imports/vmimport"),
    ("#forkliftglobal", "#migrations"),
    ("#automation", "#automation/capi"),
    ("#automation/capi", "#automation/capi"),
    ("#automation/terraform", "#automation/terraform"),
    ("#automation/pxe", "#automation/pxe"),
    ("#clusterdr", "#harvester/harv2/dr"),
    ("#drplans", "#dr"),
    ("#drlinks", "#dr/links"),
    ("#gpus", "#gpus"),
    ("#bookings", "#bookings"),
    ("#usage", "#usage"),
    ("#activity", "#activity"),
]


@pytest.fixture(scope="module")
def legacy_out():
    out = run_node(legacy=[(t, CTX) for t, _ in LEGACY_ROWS])["legacy"]
    return dict(zip([t for t, _ in LEGACY_ROWS], out))


@pytest.mark.parametrize("token,expected", LEGACY_ROWS, ids=[t for t, _ in LEGACY_ROWS])
def test_every_legacy_token_translates(legacy_out, token, expected):
    r = legacy_out[token]
    assert r is not None, f"{token} non traduit"
    back = run_node(routes=[r])["formatted"][0]
    assert back == expected


def test_legacy_table_is_pinned():
    """La table est figée : toute ligne ajoutée ou retirée passe par ce test."""
    tables = run_node()["tables"]
    got = {f"#{k}": v.replace("{L}", "harv2").replace("{LK}", "kvm-b")
           for k, v in tables["LEGACY"].items()}
    assert got == dict(LEGACY_ROWS)


def test_published_constants():
    t = run_node()["tables"]
    assert t["HARVESTER_PARAMS"] == ["nodes", "storage", "network", "addons", "security",
                                     "monlog", "advanced", "upgrade", "power", "conn"]
    assert t["KVM_PARAMS"] == KVM_PARAMS
    assert t["HARVESTER_PANES"] == HARVESTER_PANES
    assert t["IMPORT_PANES"] == IMPORT_PANES


LEGACY_CTX_CASES = [
    # dernier hôte absent : le premier visible
    ("#storage", {"harvester": ["harv1", "harv2"], "kvm": []}, "#harvester/harv1/params/storage"),
    # dernier hôte retiré de la configuration : le premier visible
    ("#storage", {"lastHarvester": "gone", "harvester": ["harv1"]}, "#harvester/harv1/params/storage"),
    ("#kvmstorage", {"lastKvm": "gone", "kvm": ["kvm-a"]}, "#kvm/kvm-a/params/pools"),
    # nom à point conservé
    ("#kvmnetworks", {"kvm": ["kvm-paris.01"]}, "#kvm/kvm-paris.01/params/networks"),
    # jeton sans hôte nécessaire : ctx vide accepté
    ("#allvms", {}, "#vms"),
    ("#forkliftglobal", None, "#migrations"),
]


@pytest.mark.parametrize("token,ctx,expected", LEGACY_CTX_CASES)
def test_legacy_picks_the_right_host(token, ctx, expected):
    r = run_node(legacy=[(token, ctx)])["legacy"][0]
    assert run_node(routes=[r])["formatted"][0] == expected


NO_HOST = [("#storage", {"harvester": [], "kvm": ["kvm-a"]}),
           ("#overview", {}),
           ("#namespaces", {"harvester": []}),
           ("#kvmstorage", {"harvester": ["harv1"], "kvm": []}),
           ("#kvmnetworks", {})]


@pytest.mark.parametrize("token,ctx", NO_HOST)
def test_legacy_without_host_points_to_hosts(token, ctx):
    assert run_node(legacy=[(token, ctx)])["legacy"][0] == {"name": "hosts", "notice": "no-host"}


@pytest.mark.parametrize("token", ["", "#", None, "#nope", "#xen/a", "#harvester/harv1"])
def test_legacy_empty_or_unknown_is_null(token):
    """Jeton vide ou inconnu : null, le routeur choisit le défaut. Une adresse
    neuve n'est pas un ancien jeton."""
    assert run_node(legacy=[(token, CTX)])["legacy"][0] is None


def test_routes_mutation_legacy_table():
    """Mutation volontaire : sans `storage` dans LEGACY, la ligne `#storage`
    échoue. Prouve que fromLegacy lit la table publiée, pas une copie."""
    tokens = [t for t, _ in LEGACY_ROWS]
    out = run_node(legacy=[(t, CTX) for t in tokens], mutate="storage")["legacy"]
    got = dict(zip(tokens, out))
    assert got["#storage"] is None, "la table mutée n'est pas relue"
    # les autres lignes tiennent toujours
    assert got["#network"] is not None and got["#allvms"] is not None


def test_panes_match_sections_def():
    """Les sous-onglets de la grammaire sont les clés de pane de Sections.DEF :
    une pane ajoutée côté sections sans la grammaire (ou l'inverse) casse ici."""
    js = r"""
      global.window = global;
      global.document = { addEventListener() {}, querySelectorAll() { return []; } };
      require('./web/static/js/sections.js');
      const R = require('./web/static/js/routes.js');
      const D = window.Sections.DEF;
      const panes = {};
      Object.keys(D).forEach((k) => { panes[k] = Object.keys(D[k].panes); });
      console.log(JSON.stringify({ panes, H: R.HARVESTER_PANES, I: R.IMPORT_PANES }));"""
    try:
        r = subprocess.run(["node", "-e", js], capture_output=True, text=True, cwd=str(ROOT), timeout=30)
    except FileNotFoundError:
        pytest.skip("node unavailable")
    assert r.returncode == 0, r.stderr[:400]
    d = json.loads(r.stdout)
    for sec, panes in d["panes"].items():
        mine = d["I"].get(sec) if sec in d["I"] else d["H"].get(sec)
        assert mine == panes, f"{sec}: grammaire {mine} != Sections.DEF {panes}"


def test_routes_module_has_no_dom_access():
    src = (ROOT / "web/static/js/routes.js").read_text()
    for needle in ("document.", "localStorage", "fetch(", "location."):
        assert needle not in src, needle
