"""v1.98.1 : version de sécurité (autorités emSign compromises, CVE-2026-111943).

L'image se construit sur une base tirée à neuf (la 1.98.0 était bâtie sur une
base vieille de six semaines qui faisait encore confiance aux autorités
emSign G3, C3 et C1), chaque binaire téléchargé est vérifié par sa somme, et
l'outil de contrôle d'image refuse un binaire Go non corrigé qui n'est pas
déclaré comme risque résiduel, à la version de Go acceptée.
"""
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CF = (ROOT / "container" / "Containerfile").read_text()
PKG = (ROOT / "package.sh").read_text()

_spec = importlib.util.spec_from_file_location(
    "check_image_trust", ROOT / "tools" / "release" / "check_image_trust.py")
cit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cit)


def _arg(name):
    m = re.search(rf"^ARG\s+{name}=(\S+)", CF, re.M)
    return m[1] if m else None


@pytest.mark.parametrize("name, value", [
    ("KUBECTL_VERSION", "v1.35.9"),
    ("KUBECTL_SHA256", "3cfeaf80be482b435b0aa214aff6e0b2c312ee23c0ff20810c75517b6004c6eb"),
    ("YQ_VERSION", "v4.44.3"),
    ("YQ_SHA256", "a2c097180dd884a8d50c956ee16a9cec070f30a7947cf4ebf87d5f36213e9ed7"),
    ("TOFU_VERSION", "1.12.9"),
    ("TOFU_SHA256", "3ef275a8ec6738741bb1d3ad0bbddd808ce4717b60a4fffdfac8e828e27ec554"),
])
def test_containerfile_pins_the_release_tools(name, value):
    assert _arg(name) == value


def test_every_downloaded_binary_is_checked_against_its_sha256():
    runs = [r for r in re.split(r"^RUN ", CF, flags=re.M) if "curl -fsSL" in r]
    assert len(runs) == 3
    for r in runs:
        assert re.search(r'echo "\$\{\w+_SHA256\}  \S+" \| sha256sum -c -', r), r[:80]


def test_base_trust_store_is_brought_up_to_date():
    assert re.search(r"zypper --non-interactive update .*ca-certificates-mozilla", CF)


def test_package_pulls_a_fresh_base_and_reports_its_digest():
    build = re.search(r'"\$RUNTIME" build ([^\n]*)', PKG)[1]
    assert "--pull=always" in build
    assert "image inspect --format '{{.Digest}}' registry.suse.com/bci/python:3.11" in PKG
    assert re.search(r"^FROM registry\.suse\.com/bci/python:3\.11$", CF, re.M)


@pytest.mark.parametrize("version, fixed", [
    ("go1.22.6", False),
    ("go1.26.0", False),
    ("go1.26.9", False),
    ("go1.26.10", True),
    ("go1.26.11", True),
    ("go1.27.2", False),
    ("go1.27.3", True),
    ("go1.28", True),
    ("go1.28.0", True),
    ("go1.28rc1", True),
    ("go2.0", True),
    ("go1.25.99", False),
    ("go?", False),
    ("", False),
    (None, False),
])
def test_go_fix_threshold(version, fixed):
    assert cit.go_is_fixed(version) is fixed


OLD_STORE = """subject=C=US, OU=emSign PKI, O=eMudhra Inc, CN=emSign ECC Root CA - C3
issuer=C=US, OU=emSign PKI, O=eMudhra Inc, CN=emSign ECC Root CA - C3
subject=C=IN, OU=emSign PKI, O=eMudhra Technologies Limited, CN=emSign ECC Root CA - G3
subject=C=US, OU=emSign PKI, O=eMudhra Inc, CN=emSign Root CA - C1
subject=C=IN, OU=emSign PKI, O=eMudhra Technologies Limited, CN=emSign Root CA - G1
subject=C=US, O=Internet Security Research Group, CN=ISRG Root X1
"""
NEW_STORE = """subject=C=IN, OU=emSign PKI, O=eMudhra Technologies Limited, CN=emSign Root CA - G1
issuer=C=US, OU=emSign PKI, O=eMudhra Inc, CN=emSign ECC Root CA - C3
subject=C=US, O=Internet Security Research Group, CN=ISRG Root X1
"""


@pytest.mark.parametrize("text, bad, kept", [
    (OLD_STORE, ["emSign ECC Root CA - C3", "emSign ECC Root CA - G3", "emSign Root CA - C1"],
     ["emSign Root CA - G1"]),
    # un issuer seul (certificat croisé) n'est pas une autorité de confiance
    (NEW_STORE, [], ["emSign Root CA - G1"]),
    ("subject=CN = emSign ECC Root CA - G3\n", ["emSign ECC Root CA - G3"], []),
    ("", [], []),
])
def test_trust_store_subjects(text, bad, kept):
    assert cit.subjects_check(text) == (bad, kept)


@pytest.mark.parametrize("name, version, verdict", [
    ("tofu", "go1.26.10", "fixed"),
    ("tofu", "go1.26.6", "unfixed"),
    ("kubectl", "go1.26.8", "residual"),
    # la 1.98.0 embarquait kubectl en Go 1.22.6 : plus accepté
    ("kubectl", "go1.22.6", "unfixed"),
    ("kubectl", "go1.27.3", "fixed"),
    ("yq", "go1.22.5", "residual"),
    ("terraform-provider-harvester", "go1.26.0", "residual"),
    ("clusterctl", "go1.26.0", "unfixed"),
])
def test_residual_risks_are_pinned_to_the_accepted_go(name, version, verdict):
    assert cit.classify(name, version) == verdict


def test_changelog_records_the_base_digest_and_residual_risks():
    log = (ROOT / "CHANGELOG.md").read_text()
    entry = log.split("## [1.98.1]", 1)[1].split("\n## [", 1)[0]
    assert re.search(r"registry\.suse\.com/bci/python:3\.11@sha256:[0-9a-f]{64}", entry)
    for word in ("emSign ECC Root CA - G3", "emSign ECC Root CA - C3", "emSign Root CA - C1",
                 "CVE-2026-111943", "kubectl", "yq", "terraform-provider-harvester",
                 "Cluster API"):
        assert word in entry, word
    assert "—" not in entry and "→" not in entry


def test_only_elf_files_are_asked_for_their_go_version(tmp_path):
    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(b"\x1f\x8b" + b"\0" * 64)
    script = tmp_path / "tool.sh"
    script.write_text("#!/bin/sh\necho go1.20\n")
    assert cit.go_version_of(archive) is None
    assert cit.go_version_of(script) is None
