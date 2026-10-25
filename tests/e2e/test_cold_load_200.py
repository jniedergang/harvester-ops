"""2.0.0 : chargement à froid sans écran parasite (décision 2).

La suite des sections actives est relevée dès l'analyse du modèle (avant
le moindre script de la page) : à l'ouverture de `#vms`, la Vue d'ensemble
n'est jamais active, et l'état du cluster (`/api/status/...`), qui ne
nourrit que la Vue d'ensemble, n'est pas lu. Une Vue d'ensemble ouverte à
froid lit bien son état, une seule fois.

Première ouverture de la 2.0.0 par un lien profond : le dernier écran de
la 1.x n'est pas rouvert plus tard, sur la première adresse vide.
"""

import re

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

from nav import goto, settle  # noqa: E402

A = "harv-fake"

# Relève chaque section active, depuis la fin de l'analyse du modèle : un
# écouteur posé AVANT les scripts de la page voit l'état d'origine du DOM.
RECORDER = """
window.__activeSeq = [];
const __note = () => {
  const ids = [...document.querySelectorAll('.tab-content.active')].map((s) => s.id).join(',');
  const seq = window.__activeSeq;
  if (seq[seq.length - 1] !== ids) seq.push(ids);
};
document.addEventListener('readystatechange', __note, { capture: true });
new MutationObserver(__note).observe(document, { subtree: true, attributes: true, attributeFilter: ['class'], childList: true });
"""


def cold(context, flask_server, hash, extra_js=""):
    context.add_init_script(
        "if (!sessionStorage.getItem('cl-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        f"localStorage.setItem('harvester_ops_current_cluster','{A}');"
        "localStorage.setItem('harvester_ops_nav2_seen','1');"
        "sessionStorage.setItem('cl-init','1'); }" + extra_js + RECORDER)
    page = context.new_page()
    calls = []
    page.on("request", lambda r: calls.append(r.url))
    goto(page, flask_server["base_url"], hash)
    page.wait_for_timeout(600)
    return page, calls


def status_calls(calls):
    return [c for c in calls if "/api/status/" in c]


def test_vms_cold_never_shows_the_overview_nor_reads_the_status(context, flask_server):
    page, calls = cold(context, flask_server, "#vms")
    seq = page.evaluate("window.__activeSeq")
    assert seq, "le relevé n'a rien vu"
    assert not any("tab-overview" in s for s in seq), seq
    assert seq[-1] == "tab-allvms", seq
    assert status_calls(calls) == []


def test_empty_address_cold_never_shows_the_overview(context, flask_server):
    page, calls = cold(context, flask_server, "")
    assert page.evaluate("location.hash") == "#vms"
    seq = page.evaluate("window.__activeSeq")
    assert not any("tab-overview" in s for s in seq), seq
    assert status_calls(calls) == []


def test_template_has_no_section_active_by_default(flask_server, context):
    page = context.new_page()
    html = page.request.get(flask_server["base_url"] + "/").text()
    assert not re.search(r'class="tab-content[^"]*\bactive\b', html)


def test_overview_cold_reads_its_status_once(context, flask_server):
    page, calls = cold(context, flask_server, f"#harvester/{A}")
    expect(page.locator("#tab-overview")).to_have_class(re.compile(r"\bactive\b"))
    assert [c.split("/api/")[-1] for c in status_calls(calls)] == [f"status/{A}"]


def test_a_deep_link_first_open_consumes_the_old_screen(context, flask_server):
    """Mineur 1 : la première ouverture de la 2.0.0 est un lien profond ;
    l'écran retenu par la 1.x ne ressort pas à la première adresse vide."""
    context.add_init_script(
        "if (!sessionStorage.getItem('dl-init')) {"
        "localStorage.setItem('harvester_ops_language','en');"
        f"localStorage.setItem('harvester_ops_current_cluster','{A}');"
        "localStorage.removeItem('harvester_ops_nav2_seen');"
        "localStorage.setItem('harvester_ops_current_tab','activity');"
        "sessionStorage.setItem('dl-init','1'); }")
    page = context.new_page()
    goto(page, flask_server["base_url"], "#gpus")
    assert page.evaluate("localStorage.getItem('harvester_ops_nav2_seen')") == "1"
    assert page.evaluate("localStorage.getItem('harvester_ops_current_tab')") is None
    page.goto(flask_server["base_url"] + "/", wait_until="domcontentloaded")
    settle(page)
    assert page.evaluate("location.hash") == "#vms"
    expect(page.locator("#tab-allvms")).to_have_class(re.compile(r"\bactive\b"))
