"""v1.55.0 : le schéma Terraform parle les cinq langues de la console.

Audit du 26/09/2026 (D14/D15) : les champs s'affichaient sous leur nom
Terraform brut (`run_strategy`, `efi`...), leurs textes n'existaient qu'en
anglais et en français, et plusieurs n'avaient pas de bulle d'aide.
"""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
LANGS = ("en", "fr", "it", "es", "de")


@pytest.fixture(scope="module")
def schema():
    js = """
      global.window = global;
      require('./web/static/js/tf-schema.js');
      const out = JSON.stringify(window.TF_SCHEMA, (k, v) => v instanceof RegExp ? String(v) : v);
      console.log(out);"""
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, cwd=str(ROOT))
    if r.returncode != 0:
        pytest.skip("node unavailable: " + r.stderr[:200])
    return json.loads(r.stdout)


def texts(schema):
    """(où, objet {langue: texte}) pour chaque libellé et description."""
    for kind, sch in schema.items():
        yield kind, sch.get("label"), sch.get("description")
        for sec in sch.get("sections") or []:
            yield f"{kind}#{sec['id']}", sec.get("label"), None
        for arg in sch.get("args") or []:
            yield f"{kind}.{arg['name']}", arg.get("label"), arg.get("description")
        for nkey, ndef in (sch.get("nested") or {}).items():
            yield f"{kind}.{nkey}", ndef.get("label"), None
            for arg in ndef.get("args") or []:
                yield f"{kind}.{nkey}.{arg['name']}", arg.get("label"), arg.get("description")


def test_every_label_and_help_exists_in_five_languages(schema):
    missing = []
    # un en-tête de bloc imbriqué n'a pas de bulle (ses champs en ont une)
    blocks = {f"{k}.{n}" for k, sch in schema.items() for n in (sch.get("nested") or {})}
    for where, label, desc in texts(schema):
        for what, obj in (("label", label), ("description", desc)):
            if what == "description" and ("#" in where or where in blocks):
                continue
            if not obj or any(not obj.get(l) for l in LANGS):
                missing.append(f"{where} {what}")
    assert missing == [], missing


def test_no_em_dash_or_unicode_arrow_in_the_texts(schema):
    blob = json.dumps(schema, ensure_ascii=False)
    assert "—" not in blob and "→" not in blob


NEW_240 = ("network", "storageclass", "ippool")


def test_the_240_kinds_have_a_label_and_a_help_on_every_control(schema):
    """2.4.0 : chaque contrôle des trois formulaires a sa bulle d'aide, une
    phrase, dans les cinq langues (mutation : retirer la description italienne
    de `ippool.range.gateway` fait échouer ce test et le précédent)."""
    seen = 0
    for where, label, desc in texts(schema):
        if where.split(".")[0].split("#")[0] not in NEW_240 or "#" in where:
            continue
        if where.count(".") == 1 and where.split(".")[1] in schema[where.split(".")[0]].get("nested", {}):
            continue
        seen += 1
        for lang in LANGS:
            assert label and label.get(lang), (where, lang)
            assert desc and desc.get(lang), (where, lang)
            assert desc[lang].rstrip().endswith("."), (where, lang)
    assert seen == 3 + 11 + 7 + 2 + 1 + 4 + 2 + 3   # genres, réseau, classe (+ topologie), pool (+ blocs)
