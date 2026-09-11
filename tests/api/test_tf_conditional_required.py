"""v1.24.0 — `network_name` est obligatoire quand l'interface est un bridge.

Signalé à l'usage : le champ s'annonçait « optionnel » à côté d'un type
`bridge`, qui est la valeur par défaut. Or c'est LÀ que se fait le
rattachement de l'interface virtuelle.

Le schéma du provider le dit autrement mais dit la même chose : le champ y
est `Optional` avec la description « if the value is empty, management
network is used », et le constructeur DÉDUIT le type de ce champ (vide
donne masquerade, renseigné donne bridge). Les deux sont donc couplés, et
l'UI laissait fabriquer la seule combinaison que le provider ne produit
jamais de lui-même : un bridge sans réseau où l'attacher.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
JS = ROOT / "web" / "static" / "js"
SCHEMA = (JS / "tf-schema.js").read_text()
FORM = (JS / "tf-form.js").read_text()


def test_network_name_is_required_for_a_bridge():
    block = SCHEMA.split("network_interface:", 1)[1].split("cloudinit:", 1)[0]
    field = block.split("name: 'network_name'", 1)[1].split("},", 1)[0]
    assert "required_when:" in field
    assert "field: 'type'" in field and "equals: 'bridge'" in field


def test_the_description_says_what_the_field_does():
    """« NetworkAttachmentDefinition (vide = management) » décrivait la
    valeur, pas la conséquence."""
    block = SCHEMA.split("network_interface:", 1)[1].split("cloudinit:", 1)[0]
    # jusqu'au champ suivant : `required_when: { ... }` contient une
    # accolade fermante qui tronquerait un découpage naïf.
    field = block.split("name: 'network_name'", 1)[1].split("name: 'wait_for_lease'", 1)[0]
    assert "bridge" in field and "masquerade" in field


def test_the_form_engine_recomputes_the_requirement_live():
    """Marquer le champ au rendu ne suffit pas : l'opérateur bascule le
    type après coup."""
    assert "data-required-field=" in FORM and "data-required-equals=" in FORM
    sync = FORM.split("const syncConditional", 1)[1].split("\n      };", 1)[0]
    assert "el.required = on;" in sync
    # la recherche du champ pilote est limitée au bloc courant : sinon
    # `disk[0].type` et `network_interface[0].type` se confondraient.
    assert "item.querySelector(" in sync
    assert ".tf-block-item" in FORM


def test_the_empty_option_stops_saying_optional_when_required():
    sync = FORM.split("const syncConditional", 1)[1].split("\n      };", 1)[0]
    assert "tf.select" in sync and "tf.optional" in sync


def test_the_placeholder_is_translated():
    """Il était écrit en dur en anglais quelle que soit la langue. Le repli
    anglais reste, pour le cas où i18n n'est pas encore chargé."""
    assert "i18n.t(phKey)" in FORM
    i18n = (JS / "i18n.js").read_text()
    for key in ("'tf.optional':", "'tf.select':"):
        assert i18n.count(key) == 5, f"{key} doit exister dans les 5 langues"
