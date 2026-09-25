"""v1.48.0 : les vues Cluster API d'avant (installation, paquets, clusters
K8S, détails) sont traduites et échappées.

Relevé en refaisant la page de création : les boutons « Details », « Scale »,
« Delete », « Build new bundle », les en-têtes de tableaux, les confirmations
et les alertes restaient en anglais dans les cinq langues, une phrase en
français dur (« Aucun cluster CAPHV managé ») s'affichait aussi en anglais,
et les noms venus du cluster étaient interpolés tels quels dans le HTML.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CAPI_JS = (ROOT / "web" / "static" / "js" / "capi.js").read_text()
I18N_JS = (ROOT / "web" / "static" / "js" / "i18n.js").read_text()

OLD_LITERALS = [
    "Loading bundles", ">Details<", "Build new bundle", "Upload bundle",
    "Aucun cluster CAPHV", "Airgap bundles", "Stack components",
    "No machines yet", "Topology spec", "Install stack from active bundle",
    "Uninstall stack", "Dry-run (preview only)", "Failed to activate bundle",
    "Failed to delete bundle", "Inspect failed", "Scale failed", "Delete failed",
    "New worker replica count", "Select a Harvester cluster first",
    "CAPI/CAPHV stack fully installed", "bundle build running",
    "used by bundles", "Tar listing empty", "Container images",
]


def test_the_old_english_literals_are_gone():
    left = [s for s in OLD_LITERALS if s in CAPI_JS]
    assert not left, f"libellés non traduits dans capi.js : {left}"


def test_table_headers_and_buttons_go_through_the_translation():
    # une cellule d'en-tête ou un bouton ne porte plus de mot anglais brut
    raw = re.findall(r"<th>([A-Z][a-z]+)</th>", CAPI_JS)
    assert set(raw) <= {"K8s"}, raw          # SHA-256 et ClusterClass restent techniques
    assert "<th>ClusterClass</th>" in CAPI_JS and "<th>SHA-256</th>" in CAPI_JS


def test_names_from_the_cluster_are_escaped():
    assert 'data-ns="${c.namespace}"' not in CAPI_JS
    assert 'data-name="${c.name}"' not in CAPI_JS
    assert 'data-filename="${b.filename}"' not in CAPI_JS
    assert "<code>${b.filename}</code>" not in CAPI_JS
    assert "${c.phase}</span>" not in CAPI_JS
    assert "${m.name}</code>" not in CAPI_JS
    assert 'data-ns="${esc(c.namespace)}"' in CAPI_JS


def test_urls_encode_the_cluster_objects():
    assert "/cluster/${namespace}/${name}" not in CAPI_JS
    assert "/cluster/${enc(namespace)}/${enc(name)}" in CAPI_JS


def test_every_new_key_exists_in_the_five_languages():
    keys = set(re.findall(r"tr\('(capi\.v\.[\w.]+)'", CAPI_JS))
    assert len(keys) > 100
    blocks = re.split(r"^\s*(?:en|fr|it|es|de):\s*\{", I18N_JS, flags=re.M)[1:6]
    assert len(blocks) == 5
    for block in blocks:
        missing = [k for k in keys if f"'{k}':" not in block]
        assert not missing, missing


def test_new_texts_keep_the_public_typography():
    for line in I18N_JS.splitlines():
        if line.strip().startswith("'capi.v."):
            assert "—" not in line and "→" not in line, line
