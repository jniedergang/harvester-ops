"""v1.26.0 — la barre des fenêtres.

Elle ne listait QUE les fenêtres minimisées. Une fenêtre ouverte mais
recouverte par une autre n'apparaissait donc nulle part : il fallait la
minimiser pour qu'elle existe enfin quelque part, ce qui est exactement
l'inverse de ce qu'on attend d'une barre de tâches.

Et les fenêtres d'une même machine répétaient toutes son nom
(« Console — default/leap156 », « Réglages — default/leap156 », …), saturant
la barre au bout de trois ouvertures.

Les fenêtres sont fabriquées par l'API publique `FloatingPanels.open`, celle
que tous les appelants utilisent, avec les mêmes `restoreSpec` que les vraies
(c'est d'eux que se déduit l'entité).
"""

import pytest

playwright = pytest.importorskip("playwright")


OPEN_PANELS = """() => {
  const mk = (id, title, icon, type, name) => FloatingPanels.open({
    id, title, icon, bodyHtml: '<p>test</p>',
    restoreSpec: { type, args: { cluster: 'harv1', namespace: 'default', name } } });
  mk('c1', 'Console — default/leap156', 'console', 'vm-console', 'leap156');
  mk('e1', 'Settings — default/leap156', 'settings', 'vm-edit', 'leap156');
  mk('s1', 'Snapshots — default/leap156', 'snapshot', 'vm-snapshots', 'leap156');
  mk('c2', 'Console — default/idp', 'console', 'vm-console', 'idp');
  FloatingPanels.open({ id: 'solo', title: 'Bundle · airgap.tar.gz',
                        icon: 'bundle', bodyHtml: '<p>x</p>' });
}"""


@pytest.fixture
def board(context, flask_server):
    page = context.new_page()
    page.context.add_init_script(
        "localStorage.setItem('harvester_ops_language','en');")
    page.goto(flask_server["base_url"], wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    page.evaluate(OPEN_PANELS)
    page.wait_for_timeout(400)
    return page


def state_of(page, fp_id):
    return page.get_attribute(f'#min-bar .min-chip[data-fp-id="{fp_id}"]',
                              'data-state')


def test_every_open_window_is_listed(board):
    """Le défaut central : une fenêtre ouverte doit être dans la barre, même
    quand elle n'est pas minimisée."""
    assert board.locator('#min-bar').is_visible()
    assert board.locator('#min-bar .min-chip').count() == 5
    for fp_id in ('c1', 'e1', 's1', 'c2', 'solo'):
        assert board.locator(
            f'#min-bar .min-chip[data-fp-id="{fp_id}"]').count() == 1, fp_id


def test_windows_of_one_entity_stack_under_its_name(board):
    """Le nom de la machine est écrit une fois, ses fenêtres derrière lui."""
    groups = board.locator('#min-bar .tb-group')
    assert groups.count() == 1, "les trois fenêtres de leap156 doivent être groupées"
    assert board.locator('#min-bar .tb-entity').inner_text() == 'default/leap156'
    # Dans un groupe, chaque élément ne porte plus que sa nature.
    labels = board.eval_on_selector_all(
        '#min-bar .tb-group .min-chip .title', 'els => els.map(e => e.textContent)')
    assert labels == ['Console', 'Settings', 'Snapshots'], labels
    # Une entité n'ayant qu'une fenêtre garde son titre complet : la
    # regrouper seule n'apporterait rien et coûterait une ligne de plus.
    assert 'default/idp' in board.locator(
        '#min-bar .min-chip[data-fp-id="c2"] .title').inner_text()


def test_clicking_the_chip_sends_the_window_away_and_brings_it_back(board):
    """L'aller-retour demandé : la puce reste en place dans les deux sens."""
    assert state_of(board, 'solo') == 'front', "la dernière ouverte est devant"

    board.click('#min-bar .min-chip[data-fp-id="solo"]')
    board.wait_for_timeout(300)
    assert board.eval_on_selector('#fp-solo', 'e => getComputedStyle(e).display') \
        == 'none'
    assert state_of(board, 'solo') == 'min'
    assert board.locator('#min-bar .min-chip[data-fp-id="solo"]').count() == 1, \
        "la puce doit rester dans la barre une fois la fenêtre rangée"

    board.click('#min-bar .min-chip[data-fp-id="solo"]')
    board.wait_for_timeout(300)
    assert board.eval_on_selector('#fp-solo', 'e => getComputedStyle(e).display') \
        == 'flex'
    assert state_of(board, 'solo') == 'front'


def test_clicking_a_covered_window_brings_it_forward(board):
    """Une fenêtre ouverte mais derrière une autre : un clic la remonte, il
    ne la range pas."""
    assert state_of(board, 'c1') == 'open'
    board.click('#min-bar .min-chip[data-fp-id="c1"]')
    board.wait_for_timeout(300)
    assert state_of(board, 'c1') == 'front'
    assert board.eval_on_selector('#fp-c1', 'e => getComputedStyle(e).display') \
        == 'flex', "elle ne doit surtout pas s'être rangée"


def test_closing_from_the_chip_removes_it(board):
    board.click('#min-bar .min-chip[data-fp-id="c2"] [data-action="close"]')
    board.wait_for_timeout(300)
    assert board.locator('#min-bar .min-chip[data-fp-id="c2"]').count() == 0
    assert board.locator('#fp-c2').count() == 0
    assert board.locator('#min-bar .min-chip').count() == 4


def test_the_bar_disappears_when_the_last_window_closes(board):
    for fp_id in ('c1', 'e1', 's1', 'c2', 'solo'):
        board.evaluate(f"FloatingPanels.close('{fp_id}')")
    board.wait_for_timeout(300)
    assert not board.locator('#min-bar').is_visible()
    # Et la réserve en bas du contenu repart avec elle.
    assert 'has-taskbar' not in (
        board.locator('body').get_attribute('class') or '')


def test_the_bar_does_not_cover_the_content(board):
    """La barre est un calque fixe : sans réserve dédiée elle mangerait les
    dernières lignes des tableaux."""
    assert 'has-taskbar' in (board.locator('body').get_attribute('class') or '')
    pad = board.eval_on_selector(
        '#content', 'e => parseInt(getComputedStyle(e).paddingBottom, 10)')
    bar = board.eval_on_selector(
        '#min-bar', 'e => e.getBoundingClientRect().height')
    assert pad >= bar, f"réserve {pad}px insuffisante pour une barre de {bar}px"
