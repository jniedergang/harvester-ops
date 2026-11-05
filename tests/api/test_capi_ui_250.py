"""2.5.0 : interface des clusters Cluster API (source).

* détails d'un cluster : ligne d'identité (restreinte ou console), texte et
  bulle traduits, valeur échappée ;
* fenêtre de création : le namespace des VMs n'est jamais envoyé (posé par
  le serveur, décision 164) ; « Enregistrer comme modèle » retire nom et
  namespaces de la demande.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CAPI = (ROOT / "web" / "static" / "js" / "capi.js").read_text()
CREATE = (ROOT / "web" / "static" / "js" / "capi-create.js").read_text()


def test_details_show_the_identity_escaped_with_a_tooltip():
    line = CAPI.split('class="capi-identity', 1)[1].split("</p>", 1)[0]
    for key in ("capi.v.details.identityRestricted", "capi.v.details.identityConsole",
                "capi.v.details.t.identityRestricted", "capi.v.details.t.identityConsole"):
        assert key in line
    assert 'data-identity="${esc(d.identity)}"' in line and "data-tip=" in line


def test_create_never_sends_the_target_namespace_and_templates_drop_plan_fields():
    body = CREATE.split("function body() {", 1)[1].split("}", 1)[0]
    assert "delete b.target_namespace" in body
    assert "const PLAN_FIELDS = ['name', 'namespace', 'target_namespace'];" in CREATE
    save = CREATE.split("q('save-template').addEventListener", 1)[1].split("});", 1)[0]
    assert "PLAN_FIELDS.forEach(k => delete spec[k])" in save and "can('admin')" in save
