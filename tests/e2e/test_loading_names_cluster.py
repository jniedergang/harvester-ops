"""v1.44.12 : le texte de chargement des vues nomme le cluster.

Avant, les vues Cluster, Réseau, Stockage et Fabrique affichaient
« Loading {name}'s topology… », le marqueur tel quel, dans toutes les
langues ; il s'est vu jusque dans l'aperçu animé du README.
"""

import threading

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).parent))
from boards import goto_board  # noqa: E402


@pytest.mark.parametrize("sub,path,lang,needle", [
    ("cluster", "**/api/topology/**", "en", "Loading harv-fake's topology"),
    ("storage", "**/api/storage-map/**", "fr", "Chargement de la topologie de harv-fake"),
    # v1.47.2 : la vue Réseau lit /api/network-fabric, plus /api/topology.
    # Retenir la mauvaise requête laissait passer la vraie réponse (« cluster
    # injoignable », le cluster de test étant fictif) : lente seule, immédiate
    # dans la suite complète une fois le cluster connu éteint, d'où un test
    # qui n'échouait que là.
    ("network", "**/api/network-fabric/**", "de", "Topologie von harv-fake wird geladen"),
])
def test_the_loading_text_names_the_cluster(context, flask_server, sub, path, lang, needle):
    held = threading.Event()

    def hang(route, request):
        # La réponse n'arrive pas : on reste sur l'état de chargement.
        held.set()

    context.add_init_script(
        f"localStorage.setItem('harvester_ops_language','{lang}');"
        "localStorage.setItem('harvester_ops_current_cluster','harv-fake');"
        "localStorage.setItem('harvester_ops_current_tab','overview');")
    page = context.new_page()
    page.route(path, hang)
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    goto_board(page, sub)
    hint = page.locator(f'[data-board="{sub}"] .fabric-body .hint').first
    expect(hint).to_contain_text(needle, timeout=8000)
    expect(hint).not_to_contain_text("{name}")
