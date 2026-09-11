"""v1.25.0 — tout ce qui vit dans `bin/` doit arriver chez le client.

`install.sh` déployait une liste figée de trois scripts. Deux versions plus
tard, `harvester-iso-remaster.sh` existait sans jamais être installé : sur un
hôte installé par ce script, l'installation bare-metal échouait sur un
script introuvable. L'installateur du provider aurait été le suivant.

Ces tests portent sur les deux voies de déploiement : `install.sh` pour
l'hôte, l'image de conteneur pour la console.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BIN = ROOT / "bin"


def helpers():
    return sorted(p.name for p in BIN.iterdir()
                  if p.is_file() and p.suffix in (".sh", ".py"))


def test_there_are_helpers_to_deploy():
    names = helpers()
    assert "harvester-shutdown.sh" in names
    assert "harvester-provider-install.py" in names


def test_install_sh_deploys_every_helper():
    """Un glob, pas une énumération : c'est ce qui empêche l'oubli suivant."""
    src = (ROOT / "install.sh").read_text()
    block = src.split("install_scripts()", 1)[1].split("\n}", 1)[0]
    assert 'bin/*.sh' in block and 'bin/*.py' in block, (
        "install_scripts() doit déployer tout bin/, sinon un helper ajouté "
        "plus tard reste sur le sol :\n" + block
    )


def test_the_image_makes_every_helper_executable():
    dockerfile = (ROOT / "container" / "Containerfile").read_text()
    assert "COPY bin/" in dockerfile
    chmod = [ln for ln in dockerfile.splitlines() if "chmod +x" in ln
             and "harvester-" in ln]
    assert chmod, "aucun chmod sur les helpers"
    line = chmod[0]
    assert "harvester-*.sh" in line and "harvester-*.py" in line, (
        "un helper non exécutable échoue au lancement avec « Permission "
        "denied », loin de sa cause : " + line
    )


def test_every_helper_is_executable_in_the_repo():
    """Le mode vient du dépôt : `COPY` le conserve, et `package.sh` copie
    `bin/` tel quel."""
    import os
    not_exec = [p.name for p in BIN.iterdir()
                if p.is_file() and p.suffix in (".sh", ".py")
                and not os.access(p, os.X_OK)]
    assert not not_exec, f"bit d'exécution manquant : {not_exec}"
