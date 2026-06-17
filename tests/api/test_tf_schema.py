"""Source-level guards for the v1.4.36 schema-driven Terraform form.

These checks parse tf-schema.js as text (not JS) and assert that:
  - every kind declared in TF_SCHEMA has a matching handler in
    _render_tf_for_kind() in app.py;
  - every ref_endpoint declared has a Flask route under that path.

The runtime e2e tests verify the form behaves correctly in the browser;
these cheap checks lock in the wiring so a typo can't slip through.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_JS = ROOT / "web" / "static" / "js" / "tf-schema.js"
TF_JS = ROOT / "web" / "static" / "js" / "tf-form.js"
APP_PY = ROOT / "web" / "app.py"


def _kinds_in_schema():
    """Extract top-level keys of TF_SCHEMA from the JS source.
    Looks for lines like `  vm: {`, `  image: {` indented exactly 2 spaces
    inside the TF_SCHEMA = { … } literal.
    """
    text = SCHEMA_JS.read_text()
    start = text.find("const TF_SCHEMA = {")
    assert start >= 0, "TF_SCHEMA literal not found in tf-schema.js"
    block = text[start:]
    return sorted(set(re.findall(r"^  ([a-z_][a-z0-9_]*): \{", block, re.MULTILINE)))


def _ref_endpoints_in_schema():
    """Extract every distinct ref_endpoint string from tf-schema.js."""
    text = SCHEMA_JS.read_text()
    return sorted(set(re.findall(r"ref_endpoint:\s*'([^']+)'", text)))


def test_every_kind_has_backend_handler():
    kinds = _kinds_in_schema()
    assert kinds, "no kinds found in TF_SCHEMA"
    app = APP_PY.read_text()
    for kind in kinds:
        # _render_tf_for_kind() dispatches with `if kind == "X":` blocks
        assert f'if kind == "{kind}"' in app, (
            f"TF_SCHEMA declares kind {kind!r} but app.py "
            f"_render_tf_for_kind() has no `if kind == \"{kind}\"` branch"
        )


def test_every_ref_endpoint_has_flask_route():
    endpoints = _ref_endpoints_in_schema()
    assert endpoints, "no ref_endpoint declarations found in TF_SCHEMA"
    app = APP_PY.read_text()
    for ep in endpoints:
        # Endpoints in the schema have the form '/api/foo'; the Flask
        # route adds '/<cluster>'. Match both forms.
        route_v1 = f'@app.route("{ep}/<cluster>")'
        route_v2 = f"@app.route('{ep}/<cluster>')"
        assert route_v1 in app or route_v2 in app, (
            f"ref_endpoint {ep!r} in TF_SCHEMA has no matching Flask route "
            f"{ep}/<cluster> in app.py"
        )


def test_tf_form_loads_schema_globally():
    """tf-form.js looks up window.TF_SCHEMA[kind] — break that and every
    form silently turns into an `Unknown resource kind` panel."""
    js = TF_JS.read_text()
    assert "window.TF_SCHEMA" in js, (
        "tf-form.js does not read window.TF_SCHEMA — the schema would be "
        "ignored at runtime"
    )


def test_tf_schema_exposes_window_global():
    """The inline boot path in index.html loads tf-schema.js as a plain
    <script> — it must set window.TF_SCHEMA so terraform.js can iterate
    it to build the kind selector."""
    js = SCHEMA_JS.read_text()
    assert "window.TF_SCHEMA" in js, (
        "tf-schema.js does not expose TF_SCHEMA on window — terraform.js "
        "would see an empty kind selector"
    )


def test_index_html_loads_tf_schema_before_terraform():
    """tf-schema.js declares the schema; terraform.js iterates
    Object.keys(window.TF_SCHEMA) to build the resource-type dropdown.
    If the order is reversed, the dropdown is empty on first paint."""
    html = (ROOT / "web" / "templates" / "index.html").read_text()
    s_idx = html.find('src="/static/js/tf-schema.js"')
    f_idx = html.find('src="/static/js/tf-form.js"')
    t_idx = html.find('src="/static/js/terraform.js"')
    assert s_idx >= 0, "tf-schema.js <script> missing from index.html"
    assert f_idx >= 0, "tf-form.js <script> missing from index.html"
    assert t_idx >= 0, "terraform.js <script> missing from index.html"
    assert s_idx < t_idx, "tf-schema.js must load BEFORE terraform.js"
    assert f_idx < t_idx, "tf-form.js must load BEFORE terraform.js"


def test_every_kind_declares_sections():
    """v1.5.0: every kind in TF_SCHEMA must declare a `sections:` array.
    The declarations UI renders one button per section on each resource
    card — a kind without sections renders an empty button strip."""
    text = SCHEMA_JS.read_text()
    for kind in _kinds_in_schema():
        idx = text.find(f"  {kind}: {{")
        assert idx > 0, kind
        slice_ = text[idx: idx + 6000]
        assert "sections: [" in slice_, (
            f"kind {kind!r} has no `sections:` array — its declaration "
            f"card would have no buttons"
        )


def test_sections_reference_declared_args_only():
    """Source-text guard: every `args: ['name', ...]` in a section
    references an arg actually declared somewhere on the kind. A typo
    silently hides a field from the UI."""
    import re as _re
    text = SCHEMA_JS.read_text()
    section_args = _re.findall(
        r"\{\s*id:\s*'([a-z]+)',[^}]*?args:\s*\[([^\]]*)\]",
        text,
    )
    declared = set(_re.findall(r"name:\s*'([a-zA-Z_][a-zA-Z0-9_]*)'", text))
    for sec_id, blob in section_args:
        names = [n.strip().strip("'\"") for n in blob.split(',') if n.strip()]
        for n in names:
            assert n in declared, (
                f"section {sec_id!r} references undeclared arg {n!r}"
            )


def test_index_html_loads_tf_declarations_and_tf_sections():
    """v1.5.0: tf-declarations.js + tf-sections.js are required by the
    new declarations UI. They must load BEFORE terraform.js, AFTER
    tf-schema.js (declarations.js calls TF_SCHEMA's defaults)."""
    html = (ROOT / "web" / "templates" / "index.html").read_text()
    s_idx = html.find('src="/static/js/tf-schema.js"')
    d_idx = html.find('src="/static/js/tf-declarations.js"')
    sec_idx = html.find('src="/static/js/tf-sections.js"')
    t_idx = html.find('src="/static/js/terraform.js"')
    assert d_idx > 0 and sec_idx > 0
    assert s_idx < d_idx, "tf-schema.js must load BEFORE tf-declarations.js"
    assert d_idx < t_idx and sec_idx < t_idx, (
        "tf-declarations.js and tf-sections.js must load BEFORE terraform.js"
    )


def test_index_html_loads_tf_decl_panel():
    """v1.5.5: tf-decl-panel.js exposes window.TFDeclPanel; terraform.js
    calls .open() to surface the declaration overlay. Must load before
    terraform.js."""
    html = (ROOT / "web" / "templates" / "index.html").read_text()
    p_idx = html.find('src="/static/js/tf-decl-panel.js"')
    t_idx = html.find('src="/static/js/terraform.js"')
    assert p_idx > 0, "tf-decl-panel.js <script> missing from index.html"
    assert p_idx < t_idx, "tf-decl-panel.js must load BEFORE terraform.js"
