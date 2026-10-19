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


def _fn(src, head):
    return src.split(head, 1)[1].split("\n  }\n", 1)[0]


def test_calendar_rows_come_from_the_inventory_pools():
    """v1.94.1 : une ligne par lot réservable des clusters affichés (pools de
    /api/gpus-all), pas seulement les lots réservés dans la semaine ; le
    filtre par cluster s'applique aux lots."""
    cal = _fn(BOOKINGS, "function calendarHtml(")
    assert "(inventoryPools || []).forEach" in cal
    assert cal.index("(inventoryPools || []).forEach") < cal.index("shown.forEach")
    assert "tr('bkg.cal.free'" in cal and "freeOf(p.inv, booked)" in cal
    render = _fn(BOOKINGS, "function render()")
    assert "(cur.pools || []).filter((p) => !cur.prefs.cluster || p.cluster === cur.prefs.cluster)" in render
    assert "fetch('/api/gpus-all')" in BOOKINGS


def test_free_now_mirrors_the_server_formula():
    # /api/bookings/options : max(0, capacity - max(used, booked))
    assert "Math.max(0, pool.capacity - Math.max(pool.used || 0, booked))" in BOOKINGS
    app = (ROOT / "web" / "app.py").read_text()
    assert 'free_now=max(0, e["capacity"] - max(e["used"], booked))' in app


def test_a_counted_booking_is_never_pinned_on_a_unit():
    badges = _fn(BOOKINGS, "function badges(")
    assert "hits.filter((b) => b.unit)" in badges                      # « Booked by » : l'unité précise
    assert "hits.filter((b) => !b.unit)" in badges                     # par nombre : au lot
    pool_part = badges.split("hits.filter((b) => !b.unit)", 1)[1]
    assert "tr('bkg.badgePoolNow'" in pool_part and "bkg.badgeNow" not in pool_part
    gpus = (JS / "gpus.js").read_text()
    assert "Bookings.badges(u, bookings, undefined, (cur.data && cur.data.pools) || [])" in gpus


def test_forms_close_on_success_and_stay_open_on_error():
    """v1.94.1 : demande acceptée, la fenêtre se ferme (l'action reste dans
    le dock, sa fin en notification) ; refusée, elle reste ouverte avec
    l'erreur."""
    ct = (JS / "containers.js").read_text()
    dlg = _fn(ct, "function dialog(")
    ok, err = dlg.split("} catch (err) {", 1)
    assert "FloatingPanels.close(id);" in ok and "follow(out.action_id, null, out.doneText);" in ok
    assert ok.index("await onSubmit(") < ok.index("FloatingPanels.close(id);")
    assert "FloatingPanels.close" not in err and "res-error" in err
    sub = BOOKINGS.split("f.addEventListener('submit'", 1)[1].split("\n    });\n", 1)[0]
    ok, err = sub.split("} catch (err) {", 1)
    assert ok.index("await send('POST', '/api/bookings', p)") < ok.index("FloatingPanels.close('bkg-new');")
    assert "follow(out.action_id, null," in ok
    assert "FloatingPanels.close" not in err and "showCheck('refused'" in err


def test_danger_button_is_solid_in_every_theme():
    css = (ROOT / "web" / "static" / "css" / "style.css").read_text()
    rule = re.search(r"^\.btn-danger \{([^}]*)\}", css, re.M).group(1)
    assert "background: var(--danger)" in rule and "color: var(--btn-danger-text)" in rule
    assert "--danger-bg:" not in css and "var(--danger-bg)" not in css
    assert re.search(r"^\.btn-danger:disabled \{[^}]*opacity", css, re.M)
    blocks = re.findall(r"^html[^{]*\{([^}]*)\}", css, re.M)
    themed = [b for b in blocks if "--danger:" in b]
    assert len(themed) == 10 and all("--btn-danger-text:" in b for b in themed)


def test_no_em_dash_nor_unicode_arrow_in_the_new_files():
    for p in (JS / "bookings.js", ROOT / "bin" / "harvester-bookings.py", ROOT / "bin" / "lib" / "hv_bookings.py",
              ROOT / "bin" / "lib" / "bookings_db.py"):
        text = p.read_text()
        assert "\u2014" not in text and "\u2192" not in text, p.name
