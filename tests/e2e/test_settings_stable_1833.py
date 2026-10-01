"""v1.83.3 : la fenêtre des réglages ne change ni de taille ni de place d'un
onglet à l'autre, et ses onglets restent où ils sont (signalé par ju)."""
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402


@pytest.mark.parametrize("width,height", [(1400, 900), (1070, 884)])
def test_settings_window_and_tabs_do_not_move(context, flask_server, width, height):
    page = context.new_page()
    page.set_viewport_size({"width": width, "height": height})
    page.goto(flask_server["base_url"] + "/", wait_until="domcontentloaded")
    page.wait_for_selector("#btn-settings")
    page.wait_for_timeout(1500)
    page.click("#btn-settings")
    modal = page.locator("#settings-modal .modal")
    expect(modal).to_be_visible()
    tabs = page.locator("#settings-modal .settings-tab:visible")
    names = [t.get_attribute("data-stab") for t in tabs.all()]

    def geometry():
        page.wait_for_timeout(400)            # fin de l'animation et du rendu
        box = modal.bounding_box()
        pos = [round(t.bounding_box()["x"]) for t in tabs.all()]
        tops = {round(t.bounding_box()["y"]) for t in tabs.all()}
        return (round(box["x"]), round(box["y"]), round(box["width"]), round(box["height"])), pos, tops

    first = None
    for name in names:
        page.click(f'#settings-modal .settings-tab[data-stab="{name}"]')
        g = geometry()
        assert len(g[2]) == 1, f"tabs on several lines on {name}"
        if first is None:
            first = g
        assert g[:2] == first[:2], f"{name}: {g} != {first}"
    # la fenêtre tient dans l'écran
    assert first[0][1] >= 0 and first[0][1] + first[0][3] <= height
