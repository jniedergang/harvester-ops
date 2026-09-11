"""v1.24.0 — retrouver le binaire du provider, et le dire quand on ne le
trouve pas.

Signalé à l'usage : l'onglet Terraform affichait « provider binary missing »
alors que le binaire était sur la machine, compilé dans le dépôt du
provider. L'application ne regardait qu'à UN emplacement, celui du livrable
packagé, et le badge ne disait pas lequel.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_provider_dirs(tmp_path, monkeypatch):
    """v1.25.0 — écarter les emplacements réels de la machine.

    Depuis que le provider peut être mis à jour depuis l'interface, un
    binaire géré peut exister pour de bon sur la machine de développement.
    Les tests « aucun provider nulle part » le trouvaient alors et
    échouaient, alors que le code était juste.
    """
    monkeypatch.setattr(wapp, "TF_PROVIDER_MANAGED", tmp_path / "no-managed-here")
    monkeypatch.setattr(wapp, "TF_PROVIDER_REPO", tmp_path / "no-bundled-here")


def test_several_locations_are_searched():
    cands = [str(c) for c in wapp._tf_provider_candidates()]
    assert len(cands) >= 4, cands
    # le nom sans suffixe d'architecture compte aussi : un provider compilé
    # localement ne s'appelle pas forcément `-amd64`.
    assert any(c.endswith("terraform-provider-harvester") for c in cands)
    assert any(c.endswith("terraform-provider-harvester-amd64") for c in cands)


def test_a_built_provider_is_found_wherever_it_sits(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVESTER_OPS_TF_PROVIDER_PATH", str(tmp_path))
    binp = tmp_path / "bin" / "terraform-provider-harvester-amd64"
    binp.parent.mkdir(parents=True)
    binp.write_text("#!/bin/sh\n")
    binp.chmod(0o755)
    assert wapp._tf_provider_binary() == binp


def test_a_non_executable_file_is_not_taken_for_a_provider(tmp_path, monkeypatch):
    """Un fichier présent mais non exécutable ferait échouer `terraform
    init` bien plus loin, avec un message incompréhensible."""
    monkeypatch.setenv("HARVESTER_OPS_TF_PROVIDER_PATH", str(tmp_path))
    binp = tmp_path / "terraform-provider-harvester"
    binp.write_text("pas un binaire")
    binp.chmod(0o644)
    assert wapp._tf_provider_binary() is None


def test_the_endpoint_says_where_it_looked(client_or_api=None):
    src = (ROOT / "web" / "app.py").read_text()
    info = src.split('def api_terraform_info', 1)[1].split("\n@app.route", 1)[0]
    assert '"provider_searched"' in info
    assert '"provider_env"' in info
    ui = (ROOT / "web" / "static" / "js" / "terraform.js").read_text()
    assert "provider_searched" in ui, "la liste doit être montrée, pas seulement servie"
    assert "tf.whereLooked" in ui
