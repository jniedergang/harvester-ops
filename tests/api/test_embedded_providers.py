"""v1.51.0 : un livrable prêt à l'emploi.

Demandé par l'exploitant : « que les releases contiennent d'embarqué le
terraform provider et le capi provider du moment ». Le livrable porte le
paquet Cluster API actif et le provider Terraform épinglé ; `install.sh` les
pose dans l'état persistant du service sans écraser un choix de l'exploitant ;
l'image embarque OpenTofu (MPL-2.0) pour que l'onglet Terraform marche dès
l'installation.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))


def install_bundles(tmp_path, script_dir, state_dir):
    src = (ROOT / "install.sh").read_text()
    fn = src[src.index("install_bundles() {"):].split("\n}\n", 1)[0] + "\n}\n"
    script = ("set -eo pipefail\nok(){ echo \"OK $*\"; }\nwarn(){ echo \"WARN $*\"; }\n"
              f"SCRIPT_DIR={script_dir}\nSTATE_DIR={state_dir}\n{fn}\ninstall_bundles\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def livrable(tmp_path, bundle="capi-bundle-20260925-turtles.tar.gz", payload=b"bundle",
             provider_version="1.9.0", corrupt=False):
    d = tmp_path / "livrable"
    (d / "bundles" / "capi").mkdir(parents=True)
    b = d / "bundles" / "capi" / bundle
    b.write_bytes(payload)
    digest = hashlib.sha256(b"other" if corrupt else payload).hexdigest()
    (d / "bundles" / "capi" / (bundle + ".sha256")).write_text(f"{digest}  {bundle}\n")
    tp = d / "bundles" / "terraform-provider" / "bin"
    tp.mkdir(parents=True)
    (tp / "terraform-provider-harvester").write_text("#!/bin/sh\n")
    (tp / "terraform-provider-harvester").chmod(0o755)
    (tp.parent / "provider.json").write_text(json.dumps({
        "version": provider_version, "source": f"embedded in harvester-ops 1.51.0",
        "binary": "/build/somewhere/terraform-provider-harvester"}))
    return d


def test_a_fresh_install_gets_both_providers_ready(tmp_path):
    state = tmp_path / "state"
    out = install_bundles(tmp_path, livrable(tmp_path), state)
    assert json.loads((state / "capi-bundles" / "active.json").read_text()) == {
        "filename": "capi-bundle-20260925-turtles.tar.gz"}
    assert (state / "capi-bundles" / "capi-bundle-20260925-turtles.tar.gz").read_bytes() == b"bundle"
    meta = json.loads((state / ".local/share/harvester-ops/terraform-provider/provider.json").read_text())
    assert meta["version"] == "1.9.0"
    assert meta["binary"] == str(state / ".local/share/harvester-ops/terraform-provider/bin/terraform-provider-harvester")
    assert os.access(meta["binary"], os.X_OK)
    assert "installed and active" in out


def test_the_operators_choices_are_kept(tmp_path):
    state = tmp_path / "state"
    (state / "capi-bundles").mkdir(parents=True)
    (state / "capi-bundles" / "active.json").write_text('{"filename": "mine.tar.gz"}')
    prov = state / ".local/share/harvester-ops/terraform-provider"
    prov.mkdir(parents=True)
    (prov / "provider.json").write_text(json.dumps({"version": "1.8.3", "source": "https://mirror/p.zip"}))
    out = install_bundles(tmp_path, livrable(tmp_path), state)
    assert json.loads((state / "capi-bundles" / "active.json").read_text())["filename"] == "mine.tar.gz"
    assert json.loads((prov / "provider.json").read_text())["version"] == "1.8.3"
    assert "the active bundle is kept" in out and "installed from the console is kept" in out


def test_a_previous_embedded_provider_is_upgraded(tmp_path):
    state = tmp_path / "state"
    prov = state / ".local/share/harvester-ops/terraform-provider"
    prov.mkdir(parents=True)
    (prov / "provider.json").write_text(json.dumps({"version": "1.7.2",
                                                    "source": "embedded in harvester-ops 1.50.0"}))
    install_bundles(tmp_path, livrable(tmp_path), state)
    assert json.loads((prov / "provider.json").read_text())["version"] == "1.9.0"


def test_a_corrupt_bundle_is_not_installed(tmp_path):
    state = tmp_path / "state"
    out = install_bundles(tmp_path, livrable(tmp_path, corrupt=True), state)
    assert "checksum mismatch" in out
    assert not (state / "capi-bundles" / "active.json").exists()


def test_the_package_embeds_them():
    src = (ROOT / "package.sh").read_text()
    assert "scripts/embedded-providers.env" in src and "SKIP_EMBEDDED" in src
    assert "bundles/capi" in src and "bundles/terraform-provider" in src
    pins = (ROOT / "scripts" / "embedded-providers.env").read_text()
    assert "TF_PROVIDER_VERSION=" in pins
    main = (ROOT / "install.sh").read_text().split("main() {", 1)[1]
    assert main.index("setup_service_account") < main.index("install_bundles") < main.index("prepare_config")


def test_the_service_keeps_its_bundles_in_writable_state():
    unit = (ROOT / "config" / "systemd" / "harvester-ops.service").read_text()
    assert "HARVESTER_OPS_CAPI_BUNDLE=/var/lib/harvester-ops/capi-bundles/capi-bundle.tar.gz" in unit
    assert "/var/lib/harvester-ops:/var/lib/harvester-ops:rw" in unit


def test_the_image_carries_opentofu_verified_and_xorriso():
    cf = (ROOT / "container" / "Containerfile").read_text()
    assert "opentofu/opentofu/releases/download" in cf and "sha256sum -c" in cf
    assert "TOFU_SHA256=" in cf and "xorriso" in cf


def test_terraform_or_opentofu_is_found(monkeypatch, tmp_path):
    import app as wapp
    tofu = tmp_path / "tofu"
    tofu.write_text("#!/bin/sh\n")
    tofu.chmod(0o755)
    monkeypatch.delenv("HARVESTER_OPS_TF_BIN", raising=False)
    monkeypatch.setattr(wapp.os.path, "isfile", lambda p: p == str(tofu))
    monkeypatch.setattr(wapp.shutil, "which", lambda n: str(tofu) if n == "tofu" else None)
    assert wapp._resolve_tf_bin() == str(tofu)
    monkeypatch.setenv("HARVESTER_OPS_TF_BIN", "/opt/terraform")
    assert wapp._resolve_tf_bin() == "/opt/terraform"
