"""v1.94.0 : le câblage des réservations dans l'interface, au niveau du source
(ce que l'e2e ne couvre pas : la fenêtre de modification d'une VM)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
JS = ROOT / "web" / "static" / "js"
BOOKINGS = (JS / "bookings.js").read_text()


def test_vm_edit_hints_gpu_vgpu_and_mig_availability_next_to_passthrough():
    src = (JS / "vm-edit.js").read_text()
    assert src.count('<div class="form-hint bkg-hint" data-bkg-hint></div>') == 1
    assert "Bookings.hint(sectionEl.querySelector('[data-bkg-hint]'), cluster, ['gpu', 'vgpu', 'mig'])" in src
    # le repère est rendu dans la section du passthrough, avant ses cartes
    firmware = src.split("function renderFirmware", 1)[1].split("\n  }\n", 1)[0]
    assert firmware.index("data-bkg-hint") < firmware.index('data-cards="hostdev"')


def test_container_form_hints_container_gpus_only():
    src = (JS / "containers.js").read_text()
    assert "Bookings.hint(root.querySelector('[data-bkg-hint]'), f.cluster.value, ['node'])" in src


def test_hints_are_read_only():
    hint = BOOKINGS.split("async function hint(", 1)[1].split("\n  }\n", 1)[0]
    assert "POST" not in hint and "send(" not in hint


def test_the_view_follows_the_tab_and_the_script_is_loaded():
    app = (JS / "app.js").read_text()
    assert "if (name === 'bookings') { if (window.Bookings) Bookings.start($('#bookings-host')); }" in app
    assert "else if (window.Bookings) Bookings.stop();" in app
    html = (ROOT / "web" / "templates" / "index.html").read_text()
    assert html.index("/static/js/gpus.js") < html.index("/static/js/bookings.js")
    assert 'id="bookings-host"' in html and 'data-tab="bookings"' in html


def test_no_raw_eventsource_no_confirm_and_escaped_holder_and_note():
    assert "new EventSource(" not in BOOKINGS
    assert not re.search(r"\bconfirm\(", BOOKINGS)
    # le texte libre (titulaire, note, cible) n'arrive dans le HTML que par esc()
    for m in re.finditer(r"\$\{(holderText\([^)]*\)|b\.note|targetText\([^)]*\))\}", BOOKINGS):
        raise AssertionError(f"unescaped {m.group(0)}")


def test_no_em_dash_nor_unicode_arrow_in_the_new_files():
    for p in (JS / "bookings.js", ROOT / "bin" / "harvester-bookings.py", ROOT / "bin" / "lib" / "hv_bookings.py",
              ROOT / "bin" / "lib" / "bookings_db.py"):
        text = p.read_text()
        assert "\u2014" not in text and "\u2192" not in text, p.name
