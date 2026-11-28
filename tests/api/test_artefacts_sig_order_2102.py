"""2.10.2 : la signature d'un paquet vaut dans les deux ordres de téléversement.

Vu au banc 2.10.1 : la `.sig` téléversée avant l'archive était effacée par
l'archive arrivée ensuite (« une ancienne signature ne vaut pas pour
celle-ci ») et le paquet était vu non signé. Règle : une `.sig` arrivée après
la dernière archive de ce nom (ou sans archive) est gardée pour l'archive qui
vient ; une `.sig` plus ancienne que l'archive remplacée appartenait à
celle-ci et part avec elle.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "tests" / "api"))

import artefacts as art  # noqa: E402
import test_artefacts_routes_260 as t260  # noqa: E402

env = t260.env
keys = t260.keys
pkg = t260.pkg
upload = t260.upload
pytestmark = t260.needs_keygen


def sign(keys, path, who="client"):
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(keys / who), "-n", art.SIG_NAMESPACE, str(path)],
                   check=True)
    return Path(str(path) + ".sig")


def staged_state(r):
    assert r.status_code == 200, r.get_json()
    return ((r.get_json() or {}).get("staged") or {}).get("trust", {}).get("state")


def incoming(env, name):
    return env["state"] / "artefacts" / "incoming" / name


@pytest.mark.parametrize("order", ["archive-then-sig", "sig-then-archive"])
def test_a_signature_counts_in_either_order(env, keys, pkg, order):
    c = env["client"]
    art.save_signers(env["state"], (keys / "signers").read_text())
    sig = sign(keys, pkg)
    if order == "archive-then-sig":
        upload(c, pkg)
        assert staged_state(upload(c, sig, name=pkg.name + ".sig")) == "valid"
    else:
        r = upload(c, sig, name=pkg.name + ".sig")
        assert r.status_code == 200 and r.get_json()["staged"] is None        # rien à vérifier encore
        assert staged_state(upload(c, pkg)) == "valid"
    assert incoming(env, pkg.name + ".sig").is_file()


def other_package(tmp_path):
    """Même nom et même version que `pkg`, autre contenu (archive A')."""
    d = tmp_path / "other"
    d.mkdir()
    (d / "ns.yaml").write_text("kind: Namespace\nmetadata: {name: other}\n")
    (d / "d.json").write_text(json.dumps({"name": "socle", "version": "2026.11", "items": [
        {"name": "ns", "kind": "manifest", "files": ["ns.yaml"]}]}))
    t260.tcli.run("pack", d / "d.json", "--out", d / "out", check=0)
    return d / "out" / "socle-2026.11.tar.gz"


def test_the_signature_of_a_replaced_archive_goes_with_it(env, keys, pkg, tmp_path):
    """Ordre réel, sans retoucher les dates : archive A, signature de A,
    puis archive A' du même nom. La signature ne vaut pas pour A' : elle
    part et le paquet est vu non signé (comme en 2.10.1), jamais refusé en
    `invalid` (422 à l'acceptation)."""
    c = env["client"]
    art.save_signers(env["state"], (keys / "signers").read_text())
    sig = sign(keys, pkg)
    upload(c, pkg)
    assert staged_state(upload(c, sig, name=pkg.name + ".sig")) == "valid"
    other = other_package(tmp_path)
    assert other.read_bytes() != pkg.read_bytes()
    assert staged_state(upload(c, other)) == "unsigned"
    assert not incoming(env, pkg.name + ".sig").exists()


def test_the_same_archive_sent_again_keeps_its_signature(env, keys, pkg):
    c = env["client"]
    art.save_signers(env["state"], (keys / "signers").read_text())
    sig = sign(keys, pkg)
    upload(c, pkg)
    upload(c, sig, name=pkg.name + ".sig")
    assert staged_state(upload(c, pkg)) == "valid"
    assert incoming(env, pkg.name + ".sig").is_file()


def test_a_new_signature_for_a_replaced_archive_is_kept(env, keys, pkg, tmp_path):
    """Archive A refusée, signature de A' puis archive A' : la signature
    vaut pour l'archive qui vient, même si A est encore en place."""
    c = env["client"]
    art.save_signers(env["state"], (keys / "signers").read_text())
    upload(c, pkg)
    other = other_package(tmp_path)
    sig = sign(keys, other)
    upload(c, sig, name=pkg.name + ".sig")
    assert staged_state(upload(c, other)) == "valid"
    assert incoming(env, pkg.name + ".sig").is_file()


def test_a_signature_of_an_unknown_key_for_the_new_archive_is_kept(env, keys, pkg, tmp_path):
    """Signature juste mais clé hors des clés de confiance : elle vaut pour
    l'archive (état `unknown-key`), elle n'est pas retirée."""
    c = env["client"]
    upload(c, pkg)
    other = other_package(tmp_path)
    sig = sign(keys, other, who="stranger")
    upload(c, sig, name=pkg.name + ".sig")
    assert staged_state(upload(c, other)) == "unknown-key"
    assert incoming(env, pkg.name + ".sig").is_file()


def test_a_wrong_signature_sent_before_its_archive_is_said_invalid(env, keys, pkg, tmp_path):
    """Sans archive en place, la `.sig` a été envoyée pour l'archive qui
    vient : fausse, elle reste et le paquet est `invalid`, jamais vu non
    signé en silence."""
    c = env["client"]
    art.save_signers(env["state"], (keys / "signers").read_text())
    sig = sign(keys, other_package(tmp_path))
    upload(c, sig, name=pkg.name + ".sig")
    assert staged_state(upload(c, pkg)) == "invalid"
    assert incoming(env, pkg.name + ".sig").is_file()
