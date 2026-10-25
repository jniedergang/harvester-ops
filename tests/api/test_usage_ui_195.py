"""v1.95.0 : le câblage de Shared resources > Usage dans l'interface, au
niveau du source (ce que l'e2e ne voit pas directement)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
JS = ROOT / "web" / "static" / "js"
USAGE = (JS / "usage.js").read_text()
HTML = (ROOT / "web" / "templates" / "index.html").read_text()


def test_the_view_follows_the_tab_and_the_script_is_loaded():
    app = (JS / "app.js").read_text()
    assert "if (name === 'usage') { if (window.Usage) Usage.start($('#usage-host')); }" in app
    assert "else if (window.Usage) Usage.stop();" in app
    assert "if (active === 'usage' && window.Usage) jobs.push(Usage.load());" in app
    assert HTML.index("/static/js/bookings.js") < HTML.index("/static/js/usage.js")
    assert 'id="usage-host"' in HTML and 'data-hnav-root="usage"' in HTML
    # troisième entrée de Shared resources, après Bookings
    # 2.0.0 : groupe Ressources partagées du menu des hôtes
    shared = HTML.split('id="hnav-shared"', 1)[1].split('</div>', 1)[0]
    assert re.findall(r'data-hnav-root="(\w+)"', shared) == ["gpus", "bookings", "usage"]


def test_no_raw_eventsource_no_confirm_no_library():
    assert "new EventSource(" not in USAGE
    assert not re.search(r"\bconfirm\(", USAGE)
    assert "import " not in USAGE and "require(" not in USAGE


def test_free_text_reaches_the_html_through_esc_only():
    # le nom d'un titulaire et d'un cluster n'arrivent dans le HTML que par esc()
    for m in re.finditer(r"\$\{(holderText\([^)]*\)|r\.holder\.name|p\.cluster|q\.holder\.name|c\.cluster)\}", USAGE):
        raise AssertionError(f"unescaped {m.group(0)}")


def test_the_chart_uses_theme_tokens():
    css = (ROOT / "web" / "static" / "css" / "style.css").read_text()
    block = css.split("/* v1.95.0 : Shared resources > Usage */", 1)[1]
    for cls in (".usg-bar-booked", ".usg-bar-consumed", ".usg-grid", ".usg-axis"):
        rule = block.split(cls + " {", 1)[1].split("}", 1)[0]
        assert "var(--" in rule and "#" not in rule, cls


def test_quota_mutations_go_through_the_tracked_routes():
    assert "send('POST', '/api/usage/quotas'" in USAGE and "send('DELETE', '/api/usage/quotas'" in USAGE
    assert USAGE.count("follow(out.action_id") == 2
    # l'éditeur ne s'affiche que pour un admin
    assert "cur.host.querySelector('[data-usg=\"quotas\"]').hidden = !admin;" in USAGE
    assert "const canQuota = admin && r.holder.kind !== 'namespace';" in USAGE


def test_quota_reasons_are_said_in_the_booking_form():
    bk = (JS / "bookings.js").read_text()
    assert "r.code === 'quota-hours'" in bk and "r.code === 'quota-units'" in bk
