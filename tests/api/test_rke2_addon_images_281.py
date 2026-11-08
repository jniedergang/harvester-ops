"""2.8.1 (banc R11) : un cluster sans Internet tire aussi les images des
modules que la console pose par ClusterResourceSet (fournisseur de cloud
Harvester, pilote CSI et ses compagnons, kube-vip).

Vu en réel sur harvlab2 : c31 (`offline: true`, repli coupé) disponible,
mais `harvester-cloud-provider`, `harvester-csi-driver` et les quatre
`longhornio/csi-*` en ImagePullBackOff (`not found` dans le registre) : le
paquet `pack --rke2` ne portait que les images de RKE2.

* `ADDON_IMAGES` = exactement les `image:` de bin/caphv-generate et de
  bin/lib/kube-vip.yaml (garde contre la dérive) ;
* `child_images` = images de RKE2 puis celles des modules, sans doublon ;
* `fetch_rke2` (pack --rke2) les met dans l'élément rke2.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import rke2_offline as ro  # noqa: E402

LISTING = "docker.io/rancher/rke2-runtime:v1.34.11-rke2r1\ndocker.io/rancher/hardened-calico:v3.32.1\n" \
          "docker.io/rancher/hardened-cilium:v1.18.0\n"


def _qualified(ref):
    first = ref.split("/", 1)[0]
    if "/" not in ref:
        ref = "library/" + ref
    elif "." in first or ":" in first or first == "localhost":
        return ref
    return "docker.io/" + ref


def _images_of(path):
    return {_qualified(m) for m in re.findall(r"^\s*image:\s*['\"]?([^\s'\"]+)", path.read_text(), re.M)}


def test_addon_images_are_the_images_the_console_lays():
    want = _images_of(ROOT / "bin" / "caphv-generate") | _images_of(ROOT / "bin" / "lib" / "kube-vip.yaml")
    assert want, "no image found in the addon sources"
    assert set(ro.ADDON_IMAGES) == want
    assert len(ro.ADDON_IMAGES) == len(set(ro.ADDON_IMAGES))


@pytest.mark.parametrize("cni,first", [("calico", "docker.io/rancher/hardened-calico:v3.32.1"),
                                       ("cilium", "docker.io/rancher/hardened-cilium:v1.18.0")])
def test_child_images_are_rke2_then_addons(cni, first):
    out = ro.child_images(LISTING, cni)
    assert out[:2] == ["docker.io/rancher/rke2-runtime:v1.34.11-rke2r1", first]
    assert out[2:] == list(ro.ADDON_IMAGES)


def test_child_images_never_repeat_an_image():
    listing = LISTING + ro.ADDON_IMAGES[0] + "\n"
    out = ro.child_images(listing, "calico")
    assert out.count(ro.ADDON_IMAGES[0]) == 1 and set(ro.ADDON_IMAGES) <= set(out)


def test_pack_rke2_carries_the_addon_images(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("hart281", ROOT / "bin" / "harvester-artefacts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tar = b"rke2-tarball"
    import hashlib
    sums = f"{hashlib.sha256(tar).hexdigest()}  rke2.linux-amd64.tar.gz\n".encode()

    def fake_fetch(url, dest, auth, cap, timeout=120, plain=()):
        name = url.rsplit("/", 1)[-1]
        body = {"rke2.linux-amd64.tar.gz": tar, "sha256sum-amd64.txt": sums, "get.rke2.io": b"#!/bin/sh\n",
                "rke2-images-all.linux-amd64.txt": LISTING.encode()}[name]
        if dest is None:
            return body, hashlib.sha256(body).hexdigest()
        Path(dest).write_bytes(body)
        return None, hashlib.sha256(body).hexdigest()

    monkeypatch.setattr(mod, "_fetch", fake_fetch)
    monkeypatch.setattr(mod, "step", lambda *a, **k: None)
    it = {"rke2": {"version": "v1.34.11+rke2r1", "cni": "calico"}}
    refs = [x["ref"] for x in mod.fetch_rke2(type("S", (), {"plain": ()})(), it, tmp_path)]
    assert set(ro.ADDON_IMAGES) <= set(refs)
    assert "docker.io/rancher/hardened-cilium:v1.18.0" not in refs
