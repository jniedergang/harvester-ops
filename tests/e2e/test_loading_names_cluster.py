"""v1.44.12 : le texte de chargement des vues nomme le cluster.

Avant, les vues Cluster, Réseau, Stockage et Fabrique affichaient
« Loading {name}'s topology… », le marqueur tel quel, dans toutes les
langues ; il s'est vu jusque dans l'aperçu animé du README.
"""

import threading

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402


@pytest.mark.parametrize("sub,path,lang,needle", [
    ("cluster", "**/api/topology/**", "en", "Loading harv-fake's topology"),
    ("storage", "**/api/storage-map/**", "fr", "Chargement de la topologie de harv-fake"),
    ("network", "**/api/topology/**", "de", "Topologie von harv-fake wird geladen"),
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
    page.click(f'[data-overview-tab="{sub}"]')
    hint = page.locator(f'.overview-subtab[data-subtab="{sub}"] .fabric-body .hint').first
    expect(hint).to_contain_text(needle, timeout=8000)
    expect(hint).not_to_contain_text("{name}")
