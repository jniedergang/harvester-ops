"""2.10.2 : la signature d'un paquet vaut dans les deux ordres de téléversement.

Vu au banc 2.10.1 : la `.sig` téléversée avant l'archive était effacée par
l'archive arrivée ensuite (« une ancienne signature ne vaut pas pour
celle-ci ») et le paquet était vu non signé. Règle : une `.sig` arrivée après
la dernière archive de ce nom (ou sans archive) est gardée pour l'archive qui
vient ; une `.sig` plus ancienne que l'archive remplacée appartenait à
celle-ci et part avec elle.
"""

import os
import subprocess
import sys
import time
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


def test_the_signature_of_a_replaced_archive_goes_with_it(env, keys, pkg):
    c = env["client"]
    art.save_signers(env["state"], (keys / "signers").read_text())
    sig = sign(keys, pkg)
    upload(c, pkg)
    assert staged_state(upload(c, sig, name=pkg.name + ".sig")) == "valid"
    # l'archive suivante arrive après la signature de la précédente
    old = time.time() - 60
    os.utime(incoming(env, pkg.name + ".sig"), (old, old))
    assert staged_state(upload(c, pkg)) == "unsigned"
    assert not incoming(env, pkg.name + ".sig").exists()


def test_a_new_signature_for_a_replaced_archive_is_kept(env, keys, pkg):
    """Archive refusée, nouvelle signature puis nouvelle archive : la
    signature arrivée après la dernière archive vaut pour la suivante."""
    c = env["client"]
    art.save_signers(env["state"], (keys / "signers").read_text())
    upload(c, pkg)
    old = time.time() - 60
    os.utime(incoming(env, pkg.name), (old, old))
    sig = sign(keys, pkg)
    upload(c, sig, name=pkg.name + ".sig")
    assert staged_state(upload(c, pkg)) == "valid"
