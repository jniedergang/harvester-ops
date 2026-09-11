"""v1.25.0 — mettre à jour le provider Terraform depuis l'interface.

Le provider livré avec le paquet vieillit plus vite que le toolkit : une
version récente de Harvester peut en exiger un plus récent. Le remplacer
demandait jusqu'ici un accès shell à l'hôte.

Trois surfaces couvertes ici :

  * `bin/harvester-provider-install.py`, l'installateur, exercé pour de vrai
    sur des archives fabriquées dans le test (pas de réseau) ;
  * la résolution du binaire actif et de sa version, qui décide de ce que
    l'écran affiche et de ce que terraform exécute ;
  * les points d'entrée HTTP, y compris leurs refus.

Ce que les tests NE font pas : télécharger depuis GitHub. La vérification en
réel contre la release amont est faite à la main et consignée dans le
CHANGELOG, conformément à la règle du projet.
"""

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
import app as wapp  # noqa: E402

INSTALLER = ROOT / "bin" / "harvester-provider-install.py"


@pytest.fixture(autouse=True)
def isolated_provider_dirs(tmp_path, monkeypatch):
    """Étanchéité : aucun test ne doit voir le provider réellement installé
    sur la machine qui fait tourner la suite.

    Sans cela, le simple fait d'avoir utilisé la fonctionnalité une fois
    suffit à faire échouer les tests — la résolution trouve le vrai binaire
    géré au lieu de celui que le test vient de fabriquer. Attrapé en
    exécutant la suite juste après l'installation réelle.
    """
    monkeypatch.setattr(wapp, "TF_PROVIDER_MANAGED", tmp_path / "no-managed-here")
    monkeypatch.delenv("HARVESTER_OPS_TF_PROVIDER_PATH", raising=False)

ELF_AMD64 = 0x3E
ELF_ARM64 = 0xB7


def fake_elf(machine=ELF_AMD64, payload=b"not really a provider"):
    """En-tête ELF 64 bits minimal, suffisant pour le contrôle d'en-tête de
    l'installateur (il ne charge jamais le binaire, terraform s'en charge)."""
    h = bytearray(64)
    h[0:4] = b"\x7fELF"
    h[4] = 2                                   # ELFCLASS64
    h[5] = 1                                   # little endian
    h[6] = 1                                   # EV_CURRENT
    h[16:18] = (2).to_bytes(2, "little")       # ET_EXEC
    h[18:20] = machine.to_bytes(2, "little")
    return bytes(h) + payload


def make_zip(path, member_name, data):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(member_name, data)
    return path


def run_installer(*args):
    r = subprocess.run([sys.executable, str(INSTALLER), *args],
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stderr, r.stdout


# ---------------------------------------------------------------------------
# L'installateur
# ---------------------------------------------------------------------------

def test_installer_is_shipped_and_runnable():
    """Il fait partie du livrable : `package.sh` copie `bin/` en entier.
    S'il manquait, l'interface le dirait mais le site airgap n'aurait plus
    aucune voie."""
    assert INSTALLER.is_file()
    assert os.access(INSTALLER, os.X_OK), "doit être exécutable"


def test_install_from_a_local_zip(tmp_path):
    src = make_zip(tmp_path / "p.zip",
                   "terraform-provider-harvester_v1.7.3", fake_elf())
    dest = tmp_path / "managed"
    rc, err, out = run_installer(str(src), "--dest", str(dest))
    assert rc == 0, err

    binary = dest / "bin" / "terraform-provider-harvester"
    assert binary.is_file()
    assert os.access(binary, os.X_OK), "terraform exécute ce fichier"

    meta = json.loads((dest / "provider.json").read_text())
    assert meta["version"] == "1.7.3", "la version se lit dans le nom du membre"
    assert meta["sha256"] and len(meta["sha256"]) == 64
    assert meta["source"] == str(src)
    # Les étapes doivent être exploitables par la chaîne SSE.
    assert "STEP_EVENT|install|done|" in err


def test_install_from_a_bare_binary(tmp_path):
    """Un opérateur peut n'avoir que le binaire, sans l'archive. La version
    n'est alors nulle part : elle doit être fournie, pas devinée."""
    src = tmp_path / "terraform-provider-harvester"
    src.write_bytes(fake_elf())
    dest = tmp_path / "managed"

    rc, err, _ = run_installer(str(src), "--dest", str(dest))
    assert rc == 1
    assert "version indéterminable" in err

    rc, err, _ = run_installer(str(src), "--dest", str(dest), "--version", "1.6.0")
    assert rc == 0, err
    assert json.loads((dest / "provider.json").read_text())["version"] == "1.6.0"


def test_a_wrong_checksum_aborts_before_installing(tmp_path):
    src = make_zip(tmp_path / "p.zip",
                   "terraform-provider-harvester_v1.7.3", fake_elf())
    dest = tmp_path / "managed"
    rc, err, _ = run_installer(str(src), "--dest", str(dest),
                               "--sha256", "0" * 64)
    assert rc == 1
    assert "empreinte incorrecte" in err
    assert not (dest / "bin").exists(), "rien ne doit être posé sur un écart"


def test_a_zip_that_tries_to_escape_is_refused(tmp_path):
    """Zip-slip : un membre dont le nom remonte hors du répertoire. Le
    membre est ignoré, donc l'archive ne contient plus rien d'installable."""
    src = tmp_path / "evil.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("../../terraform-provider-harvester", fake_elf())
    dest = tmp_path / "managed"
    rc, err, _ = run_installer(str(src), "--dest", str(dest))
    assert rc == 1
    assert "aucun binaire de provider" in err
    assert not (tmp_path.parent / "terraform-provider-harvester").exists()


def test_a_binary_of_the_wrong_architecture_is_refused(tmp_path):
    """Sans ce contrôle, terraform échoue bien plus loin sur un « exec
    format error » qui ne désigne pas la cause."""
    src = make_zip(tmp_path / "p.zip",
                   "terraform-provider-harvester_v1.7.3",
                   fake_elf(machine=ELF_ARM64))
    dest = tmp_path / "managed"
    rc, err, _ = run_installer(str(src), "--dest", str(dest), "--arch", "amd64")
    assert rc == 1
    assert "arm64" in err and "amd64" in err
    assert not (dest / "bin" / "terraform-provider-harvester").exists()


def test_an_html_error_page_is_named_as_such(tmp_path):
    """Cas le plus fréquent d'un miroir mal configuré : on télécharge une
    page d'erreur au lieu du fichier."""
    src = tmp_path / "provider.zip"
    src.write_bytes(b"<html><body>404 Not Found</body></html>")
    rc, err, _ = run_installer(str(src), "--dest", str(tmp_path / "managed"))
    assert rc == 1
    assert "format non reconnu" in err


def test_reinstalling_replaces_the_previous_binary(tmp_path):
    dest = tmp_path / "managed"
    first = make_zip(tmp_path / "a.zip",
                     "terraform-provider-harvester_v1.6.0", fake_elf())
    second = make_zip(tmp_path / "b.zip",
                      "terraform-provider-harvester_v1.7.3",
                      fake_elf(payload=b"a different build entirely"))
    assert run_installer(str(first), "--dest", str(dest))[0] == 0
    assert run_installer(str(second), "--dest", str(dest))[0] == 0
    meta = json.loads((dest / "provider.json").read_text())
    assert meta["version"] == "1.7.3"
    body = (dest / "bin" / "terraform-provider-harvester").read_bytes()
    assert b"a different build entirely" in body


# ---------------------------------------------------------------------------
# Résolution du binaire actif et de sa version
# ---------------------------------------------------------------------------

def install_managed(tmp_path, monkeypatch, version="1.7.3"):
    managed = tmp_path / "managed"
    binary = managed / "bin" / "terraform-provider-harvester"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(fake_elf())
    binary.chmod(0o755)
    (managed / "provider.json").write_text(json.dumps({
        "version": version, "sha256": "ab" * 32, "source": "test",
        "arch": "amd64", "size": binary.stat().st_size,
        "installed_at": 1_700_000_000, "binary": str(binary),
    }))
    monkeypatch.setattr(wapp, "TF_PROVIDER_MANAGED", managed)
    monkeypatch.delenv("HARVESTER_OPS_TF_PROVIDER_PATH", raising=False)
    return managed, binary


def test_a_managed_install_wins_over_the_bundled_provider(tmp_path, monkeypatch):
    """Le cœur de la fonctionnalité : si le provider du livrable passait
    devant, la mise à jour serait posée sur le disque et sans aucun effet."""
    bundled = tmp_path / "bundled"
    (bundled / "bin").mkdir(parents=True)
    bp = bundled / "bin" / "terraform-provider-harvester-amd64"
    bp.write_bytes(fake_elf())
    bp.chmod(0o755)
    monkeypatch.setattr(wapp, "TF_PROVIDER_REPO", bundled)

    assert wapp._tf_provider_binary() == bp

    managed, binary = install_managed(tmp_path, monkeypatch)
    assert wapp._tf_provider_binary() == binary
    assert wapp._tf_provider_version() == "v1.7.3"
    assert wapp._tf_provider_origin(binary) == "managed"
    assert wapp._tf_provider_origin(bp) == "bundled"


def test_an_explicit_override_still_wins_over_a_managed_install(tmp_path,
                                                                monkeypatch):
    """L'opérateur qui pointe explicitement une variable d'environnement
    doit rester maître : c'est sa porte de sortie quand tout le reste se
    trompe."""
    install_managed(tmp_path, monkeypatch)
    override = tmp_path / "override"
    (override / "bin").mkdir(parents=True)
    op = override / "bin" / "terraform-provider-harvester"
    op.write_bytes(fake_elf())
    op.chmod(0o755)
    monkeypatch.setenv("HARVESTER_OPS_TF_PROVIDER_PATH", str(override))
    assert wapp._tf_provider_binary() == op
    assert wapp._tf_provider_origin(op) == "custom"


def test_a_binary_unzipped_by_hand_is_found_and_dated(tmp_path, monkeypatch):
    """Dézipper l'archive officielle à la main donne exactement
    `terraform-provider-harvester_v1.7.3`. Ce nom n'était cherché nulle
    part : le binaire était sur la machine et l'écran disait « missing ».
    Il porte sa version, qui n'a donc besoin d'aucune fiche."""
    root = tmp_path / "elsewhere"
    root.mkdir()
    monkeypatch.setenv("HARVESTER_OPS_TF_PROVIDER_PATH", str(root))
    monkeypatch.setattr(wapp, "TF_PROVIDER_MANAGED", tmp_path / "absent")
    monkeypatch.setattr(wapp, "TF_PROVIDER_REPO", tmp_path / "absent-too")
    binary = root / "terraform-provider-harvester_v1.7.3"
    binary.write_bytes(fake_elf())
    binary.chmod(0o755)
    assert wapp._tf_provider_binary() == binary
    assert wapp._tf_provider_version() == "v1.7.3"


def test_the_most_recent_of_several_unzipped_versions_wins(tmp_path,
                                                            monkeypatch):
    """Deux versions déposées côte à côte : c'est celle qu'on vient de
    poser qui doit servir, pas celle que l'ordre alphabétique désigne."""
    root = tmp_path / "elsewhere"
    root.mkdir()
    monkeypatch.setenv("HARVESTER_OPS_TF_PROVIDER_PATH", str(root))
    monkeypatch.setattr(wapp, "TF_PROVIDER_MANAGED", tmp_path / "absent")
    monkeypatch.setattr(wapp, "TF_PROVIDER_REPO", tmp_path / "absent-too")
    old = root / "terraform-provider-harvester_v1.9.0"
    new = root / "terraform-provider-harvester_v1.7.3"
    for p in (old, new):
        p.write_bytes(fake_elf())
        p.chmod(0o755)
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_800_000_000, 1_800_000_000))
    assert wapp._tf_provider_binary() == new
    assert wapp._tf_provider_version() == "v1.7.3"


def test_a_binary_without_a_version_in_its_name_falls_back(tmp_path,
                                                            monkeypatch):
    root = tmp_path / "elsewhere"
    root.mkdir()
    monkeypatch.setenv("HARVESTER_OPS_TF_PROVIDER_PATH", str(root))
    monkeypatch.setattr(wapp, "TF_PROVIDER_MANAGED", tmp_path / "absent")
    monkeypatch.setattr(wapp, "TF_PROVIDER_REPO", tmp_path / "absent-too")
    binary = root / "terraform-provider-harvester"
    binary.write_bytes(fake_elf())
    binary.chmod(0o755)
    # Le nom ne dit rien, et aucun dépôt git n'existe : « dev », pas une
    # version inventée.
    assert wapp._tf_provider_version() == "dev"


def test_no_provider_at_all_reports_dev_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVESTER_OPS_TF_PROVIDER_PATH", str(tmp_path / "nope"))
    monkeypatch.setattr(wapp, "TF_PROVIDER_MANAGED", tmp_path / "absent")
    monkeypatch.setattr(wapp, "TF_PROVIDER_REPO", tmp_path / "absent-too")
    assert wapp._tf_provider_binary() is None
    assert wapp._tf_provider_version() == "dev"
    assert wapp._tf_provider_origin(None) == ""


# ---------------------------------------------------------------------------
# Invalidation des workspaces
# ---------------------------------------------------------------------------

def test_changing_provider_forces_a_fresh_init_but_keeps_state(tmp_path,
                                                                monkeypatch):
    """Sans ce ménage, la mise à jour n'aurait aucun effet : l'apply ne
    relance `terraform init` que si `.terraform/` est absent, et le miroir
    local garde une copie du greffon.

    Ce qui doit survivre : le state. Le perdre ferait recréer des VMs déjà
    existantes au prochain apply.
    """
    ws = tmp_path / "workspaces"
    harv = ws / "harv1"
    (harv / ".terraform" / "providers").mkdir(parents=True)
    (harv / "plugins" / "registry.terraform.io").mkdir(parents=True)
    (harv / ".terraform.lock.hcl").write_text('provider "harvester" {}\n')
    (harv / "terraform.tfstate").write_text('{"version": 4}')
    (harv / "vm_demo.tf").write_text("resource {}")
    (harv / "vm_demo.json").write_text("{}")
    monkeypatch.setattr(wapp, "TF_WORKSPACES", ws)

    touched = wapp._tf_workspaces_invalidate()

    assert touched == ["harv1"]
    assert not (harv / ".terraform").exists()
    assert not (harv / "plugins").exists()
    assert not (harv / ".terraform.lock.hcl").exists()
    assert (harv / "terraform.tfstate").read_text() == '{"version": 4}'
    assert (harv / "vm_demo.tf").exists()
    assert (harv / "vm_demo.json").exists()


def test_invalidation_is_silent_when_there_is_nothing_to_clean(tmp_path,
                                                               monkeypatch):
    ws = tmp_path / "workspaces"
    (ws / "harv1").mkdir(parents=True)
    monkeypatch.setattr(wapp, "TF_WORKSPACES", ws)
    assert wapp._tf_workspaces_invalidate() == []


# ---------------------------------------------------------------------------
# Points d'entrée HTTP
# ---------------------------------------------------------------------------

@pytest.fixture
def spawned(monkeypatch):
    """Intercepte le lancement de l'action : les tests de contrat ne doivent
    déclencher aucun téléchargement."""
    calls = []

    def fake_track(label, cluster, worker, *args):
        calls.append({"label": label, "worker": worker, "args": args})
        return "deadbeef0000"

    monkeypatch.setattr(wapp, "track_action", fake_track)
    return calls


def test_info_exposes_what_the_update_panel_needs(tmp_path, monkeypatch):
    install_managed(tmp_path, monkeypatch)
    with wapp.app.test_client() as c:
        d = c.get("/api/terraform/info").get_json()
    for k in ("provider_origin", "provider_managed_dir", "provider_can_install",
              "provider_installed_sha256", "provider_installed_source",
              "provider_installed_at", "provider_arch"):
        assert k in d, f"clé absente : {k}"
    assert d["provider_origin"] == "managed"
    assert d["provider_version"] == "v1.7.3"
    assert d["provider_can_install"] is True


def test_install_accepts_a_version(spawned):
    with wapp.app.test_client() as c:
        r = c.post("/api/terraform/provider/install", json={"source": "1.7.3"})
    assert r.status_code == 202
    assert r.get_json()["action_id"] == "deadbeef0000"
    assert spawned[0]["args"][0] == "1.7.3"


def test_install_accepts_an_https_url(spawned):
    url = "https://mirror.example/terraform-provider-harvester_1.7.3.zip"
    with wapp.app.test_client() as c:
        r = c.post("/api/terraform/provider/install", json={"source": url})
    assert r.status_code == 202
    assert spawned[0]["args"][0] == url


def test_install_refuses_a_local_path(spawned):
    """Garde de sécurité : accepter un chemin donnerait à tout compte
    authentifié le moyen de faire installer, donc exécuter, un fichier
    arbitraire de l'hôte comme provider. Le téléversement reste la voie
    pour un fichier local, parce que le serveur l'écrit lui-même."""
    for bad in ("/etc/passwd", "../../etc/shadow", "file:///etc/passwd",
                "~/provider.zip"):
        with wapp.app.test_client() as c:
            r = c.post("/api/terraform/provider/install", json={"source": bad})
        assert r.status_code == 400, bad
    assert spawned == []


def test_install_refuses_a_malformed_checksum(spawned):
    with wapp.app.test_client() as c:
        r = c.post("/api/terraform/provider/install",
                   json={"source": "1.7.3", "sha256": "nope"})
    assert r.status_code == 400
    assert "sha256" in r.get_json()["error"]
    assert spawned == []


def test_install_requires_a_source(spawned):
    with wapp.app.test_client() as c:
        r = c.post("/api/terraform/provider/install", json={})
    assert r.status_code == 400
    assert spawned == []


def test_upload_stages_the_file_and_passes_its_path(tmp_path, monkeypatch,
                                                     spawned):
    import io
    monkeypatch.setattr(wapp, "TF_PROVIDER_MANAGED", tmp_path / "managed")
    payload = make_zip(tmp_path / "p.zip",
                       "terraform-provider-harvester_v1.7.3", fake_elf())
    with wapp.app.test_client() as c:
        r = c.post("/api/terraform/provider/upload",
                   data={"file": (io.BytesIO(payload.read_bytes()), "p.zip")},
                   content_type="multipart/form-data")
    assert r.status_code == 202, r.get_data(as_text=True)
    staged = Path(spawned[0]["args"][0])
    assert staged.is_file()
    # Nom imposé par le serveur : le nom client ne sert jamais de chemin.
    assert staged.name.startswith("upload-") and staged.name.endswith(".bin")
    # Le fichier intermédiaire est nettoyé par le worker.
    assert spawned[0]["args"][3] == str(staged)
    # La provenance enregistrée doit être lisible : le chemin de transit ne
    # dirait rien à l'opérateur qui relit la fiche six mois plus tard.
    assert spawned[0]["args"][4] == "uploaded: p.zip"


def test_an_uploaded_filename_never_becomes_a_path(tmp_path, monkeypatch,
                                                    spawned):
    """Le nom vient du client : il ne doit ni construire un chemin ni
    ressortir tel quel dans la fiche."""
    import io
    monkeypatch.setattr(wapp, "TF_PROVIDER_MANAGED", tmp_path / "managed")
    with wapp.app.test_client() as c:
        r = c.post("/api/terraform/provider/upload",
                   data={"file": (io.BytesIO(b"x" * 32),
                                  "../../etc/cron.d/evil;rm -rf.zip")},
                   content_type="multipart/form-data")
    assert r.status_code == 202
    staged = Path(spawned[0]["args"][0])
    assert staged.parent == tmp_path / "managed" / "incoming"
    label = spawned[0]["args"][4]
    assert "/" not in label and ";" not in label and " rf" not in label


def test_upload_refuses_an_empty_file(tmp_path, monkeypatch, spawned):
    import io
    monkeypatch.setattr(wapp, "TF_PROVIDER_MANAGED", tmp_path / "managed")
    with wapp.app.test_client() as c:
        r = c.post("/api/terraform/provider/upload",
                   data={"file": (io.BytesIO(b""), "empty.zip")},
                   content_type="multipart/form-data")
    assert r.status_code == 400
    assert spawned == []


def test_upload_without_a_file_says_so(spawned):
    with wapp.app.test_client() as c:
        r = c.post("/api/terraform/provider/upload", data={},
                   content_type="multipart/form-data")
    assert r.status_code == 400
    assert "file" in r.get_json()["error"]


def test_revert_removes_the_managed_install(tmp_path, monkeypatch):
    managed, binary = install_managed(tmp_path, monkeypatch)
    ws = tmp_path / "workspaces"
    (ws / "harv1" / ".terraform").mkdir(parents=True)
    monkeypatch.setattr(wapp, "TF_WORKSPACES", ws)

    with wapp.app.test_client() as c:
        r = c.delete("/api/terraform/provider")
    assert r.status_code == 200
    d = r.get_json()
    assert d["reverted"] is True
    assert d["workspaces_reset"] == ["harv1"]
    assert not managed.exists()


def test_revert_without_a_managed_install_is_a_404(tmp_path, monkeypatch):
    monkeypatch.setattr(wapp, "TF_PROVIDER_MANAGED", tmp_path / "nothing-here")
    with wapp.app.test_client() as c:
        r = c.delete("/api/terraform/provider")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

def test_the_panel_is_wired_to_the_endpoints():
    js = (ROOT / "web" / "static" / "js" / "terraform.js").read_text()
    assert "/api/terraform/provider/install" in js
    assert "/api/terraform/provider/upload" in js
    assert "tf-prov-form" in js and "tf-prov-file-form" in js
    # Le panneau doit dire d'où vient le binaire actif, sinon l'opérateur ne
    # sait pas si sa mise à jour a pris.
    assert "provider_origin" in js
