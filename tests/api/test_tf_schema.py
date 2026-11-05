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
    import sys
    sys.path.insert(0, str(ROOT / "web"))
    import tf_kvm
    import tf_store
    for kind in kinds:
        if kind.startswith("kvm_"):
            # 2.3.0 : ressources KVM, rendues par tf_kvm et connues du magasin
            assert kind in tf_store.KVM_KINDS
            assert kind in tf_kvm.RENDERERS or kind == "kvm_rke2"
            continue
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
    v_idx = html.find('src="/static/js/tf-decl-view.js"')
    t_idx = html.find('src="/static/js/terraform.js"')
    assert d_idx > 0 and v_idx > 0
    assert s_idx < d_idx, "tf-schema.js must load BEFORE tf-declarations.js"
    assert d_idx < v_idx < t_idx, (
        "tf-declarations.js, then tf-decl-view.js, must load BEFORE terraform.js"
    )


def test_the_stacked_declaration_windows_are_gone():
    """v1.55.0 : la vue des déclarations remplace les fenêtres empilées
    (une par déclaration, une par section)."""
    html = (ROOT / "web" / "templates" / "index.html").read_text()
    assert "tf-decl-panel.js" not in html and "tf-sections.js" not in html
    assert not (ROOT / "web" / "static" / "js" / "tf-decl-panel.js").exists()


def test_every_ref_url_has_flask_route():
    """2.3.0 : les listes des formulaires KVM (`ref_url` à {target}) lisent
    les routes d'hôte KVM existantes."""
    urls = sorted(set(re.findall(r"ref_url:\s*'([^']+)'", SCHEMA_JS.read_text())))
    assert urls, "no ref_url in TF_SCHEMA"
    app = APP_PY.read_text()
    for url in urls:
        assert "{target}" in url
        assert f'@app.route("{url.replace("{target}", "<host>")}")' in app, url


# ---------------------------------------------------------------------------
# 2.4.0 : réseau de VM, classe de stockage, pool d'adresses. Champs = ceux du
# provider Harvester 1.9.0 (`terraform providers schema -json`, constats du
# plan 2026-11-03), noms identiques ; rien d'autre.
# ---------------------------------------------------------------------------
import json  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

PROVIDER_240 = {
    "network": ({"name", "namespace", "cluster_network_name", "vlan_id", "description", "route_mode",
                 "route_cidr", "route_gateway", "route_dhcp_server_ip", "config", "labels"}, {}),
    # `match_label_expressions` : contenu du bloc `allowed_topologies` (au plus un)
    "storageclass": ({"name", "parameters", "volume_provisioner", "reclaim_policy", "volume_binding_mode",
                      "allow_volume_expansion", "is_default"},
                     {"match_label_expressions": {"key", "values"}}),
    # `scope` : sous-bloc de `selector`, à plat dans le formulaire (un niveau de bloc)
    "ippool": ({"name"}, {"range": {"start", "end", "subnet", "gateway"},
                          "selector": {"network", "priority"},
                          "scope": {"guest_cluster", "namespace", "project"}}),
}


@pytest.fixture(scope="module")
def schema_json():
    js = """
      global.window = global;
      require('./web/static/js/tf-schema.js');
      console.log(JSON.stringify(window.TF_SCHEMA, (k, v) => v instanceof RegExp ? String(v) : v));"""
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode != 0:
        pytest.skip("node unavailable: " + r.stderr[:200])
    return json.loads(r.stdout)


@pytest.mark.parametrize("kind", sorted(PROVIDER_240))
def test_240_forms_carry_exactly_the_provider_fields(schema_json, kind):
    args, nested = PROVIDER_240[kind]
    sch = schema_json[kind]
    assert sch["family"] == "harvester"
    assert {a["name"] for a in sch["args"]} == args
    assert {k: {a["name"] for a in v["args"]} for k, v in (sch.get("nested") or {}).items()} == nested
    shown = {a for s in sch["sections"] for a in s.get("args", [])} | {s["nested"] for s in sch["sections"]
                                                                       if s.get("nested")}
    assert shown == args | set(nested), "every field sits in a section"


@pytest.mark.parametrize("kind, arg, want", [
    ("network", "vlan_id", {"type": "int", "required": True, "min": 0, "max": 4094}),
    ("network", "labels", {"type": "map"}),
    ("network", "route_mode", {"type": "enum", "enum_values": ["auto", "manual"]}),
    ("network", "route_cidr", {"required_when": {"field": "route_mode", "equals": "manual"}}),
    ("network", "route_gateway", {"required_when": {"field": "route_mode", "equals": "manual"}}),
    ("storageclass", "parameters", {"type": "map", "required": True}),
    ("storageclass", "is_default", {"type": "bool", "default": False}),
    ("storageclass", "allow_volume_expansion", {"type": "bool", "default": True}),
    ("storageclass", "reclaim_policy", {"enum_values": ["Delete", "Retain"]}),
    ("storageclass", "volume_binding_mode", {"enum_values": ["Immediate", "WaitForFirstConsumer"]}),
])
def test_240_field_settings(schema_json, kind, arg, want):
    a = next(x for x in schema_json[kind]["args"] if x["name"] == arg)
    assert {k: a.get(k) for k in want} == want


def test_storage_class_proposes_migratable_true(schema_json):
    p = next(x for x in schema_json["storageclass"]["args"] if x["name"] == "parameters")
    assert p["default"]["migratable"] == "true"


def test_ippool_blocks_bounds(schema_json):
    n = schema_json["ippool"]["nested"]
    assert (n["range"]["min"], n["selector"]["max"]) == (1, 1)
    assert all(a.get("required") for a in n["range"]["args"])


def test_the_declaration_view_offers_the_240_kinds():
    js = (ROOT / "web" / "static" / "js" / "tf-decl-view.js").read_text()
    assert "harvester: ['vm', 'image', 'ssh_key', 'network', 'storageclass', 'ippool', 'raw']" in js


def test_tf_form_renders_and_reads_a_map_field():
    """Le champ `map` (paires clé-valeur) : rendu de l'objet par défaut,
    lecture en objet, ligne vide ignorée, valeurs échappées."""
    js = TF_JS.read_text()
    assert "case 'map':" in js and "if (arg.type === 'map')" in js
    assert "Object.keys(v).length > 0" in js        # un objet vide n'est pas « rempli »
    assert 'value="${esc(key)}"' in js and 'value="${esc(value)}"' in js
