"""Relecture finale 2.10.1 (I-2) : une demande peut nommer l'image par son
nom affiché (« namespace/nom affiché », ce qu'écrit un script, un modèle ou
un plan). Le webhook de Harvester 1.9.1 vérifie `get` sur l'objet réel
(`image-xxxxx`) : la demande est ramenée à la référence de l'objet avant la
règle de l'identité, ses revues d'accès et le rendu, et refusée quand le nom
affiché désigne plusieurs images."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "tests" / "api"))
import capi_cluster as cc  # noqa: E402
import capi_identity as ci  # noqa: E402
from test_capi_identity_250 import Kube, demand  # noqa: E402

V = "v0.10.1"
INV = json.loads((ROOT / "tests" / "fixtures" / "capi" / "harv1-inventory-2026-11-05.json").read_text())


@pytest.fixture(scope="module")
def cli():
    s = importlib.util.spec_from_file_location("harvester_capi_2101_img", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod


class K(Kube):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.asked = []

    def create_many(self, objs):
        self.asked.extend(objs)
        return [{"status": {"allowed": True}} for _ in objs]


def _image_rule(spec):
    return [r for d in ci.docs(spec, V) for r in d.get("rules") or []
            if r["resources"] == ["virtualmachineimages"] and "get" in r["verbs"]]


def _inv(extra=()):
    inv = json.loads(json.dumps(INV))
    inv["images"] += list(extra)
    return inv


@pytest.mark.parametrize("label,image,ref", [
    ("display name", "default/sles15-sp7-minimal-vm.x86_64-cloud-qu2.qcow2", "default/image-nhtf9"),
    ("display name with spaces", "default/Rocky Linux 8 GenericCloud", "default/rocky8-genericcloud"),
    ("object name", "default/image-nhtf9", "default/image-nhtf9"),
])
def test_the_image_is_brought_back_to_its_object(cli, monkeypatch, label, image, ref):
    monkeypatch.setattr(cli, "inventory", lambda kube, desc: _inv())
    spec = demand(image=image, ssh_keypair="default/capi-ssh-key")
    k = K()
    out = cli._check(k, spec, None, "restricted")
    assert "image-missing" not in [f["code"] for f in out["findings"]], label
    assert out["spec"] is spec and spec["image"] == ref, label          # create et le rendu relisent `spec`
    assert _image_rule(spec)[0]["resourceNames"] == [ref.partition("/")[2]], label
    imgs = [o["spec"]["resourceAttributes"] for o in k.asked
            if o["spec"]["resourceAttributes"].get("resource") == "virtualmachineimages"]
    assert {a.get("name") for a in imgs if a["verb"] == "get"} == {ref.partition("/")[2]}, label


def test_a_display_name_shared_by_two_images_is_refused(cli, monkeypatch):
    twin = dict(next(i for i in INV["images"] if i["ref"] == "default/image-nhtf9"),
                ref="default/image-twin1", name="image-twin1")
    monkeypatch.setattr(cli, "inventory", lambda kube, desc: _inv([twin]))
    image = "default/sles15-sp7-minimal-vm.x86_64-cloud-qu2.qcow2"
    spec = demand(image=image, ssh_keypair="default/capi-ssh-key")
    out = cli._check(K(), spec, None, "restricted")
    f = next(f for f in out["findings"] if f["code"] == "image-ambiguous")
    assert f["level"] == "block" and out["blocked"]
    assert "default/image-nhtf9" in f["facts"]["refs"] and "default/image-twin1" in f["facts"]["refs"]
    assert spec["image"] == image                                       # jamais choisie au hasard


def test_an_unknown_image_stays_refused(cli, monkeypatch):
    monkeypatch.setattr(cli, "inventory", lambda kube, desc: _inv())
    spec = demand(image="default/no such image", ssh_keypair="default/capi-ssh-key")
    out = cli._check(K(), spec, None, "restricted")
    assert "image-missing" in [f["code"] for f in out["findings"]] and out["blocked"]
    assert spec["image"] == "default/no such image"


def test_the_finding_is_said_in_the_creation_window():
    js = (ROOT / "web" / "static" / "js" / "capi-create.js").read_text()
    assert "'image-ambiguous'" in js


# Relecture finale I-3 : le correctif manuel documenté trouve le Role par ses
# étiquettes, et le texte nomme le Role que la console écrit vraiment
# (`caphv-identity` dans le namespace du cluster, `caphv-identity-<ns>`
# partout ailleurs, `<ns>-vms` compris).
@pytest.mark.parametrize("image_ns,role", [
    ("web", "caphv-identity"),
    ("web-vms", "caphv-identity-web"),
    ("default", "caphv-identity-web"),
])
def test_the_documented_fix_finds_the_identity_role(image_ns, role):
    spec = dict(demand(image=f"{image_ns}/image-abc"), namespace="web")
    sel = {ci.L_CLUSTER: "web", ci.L_NAMESPACE: "web"}
    roles = [d["metadata"]["name"] for d in ci.docs(spec, V)
             if d["kind"] == "Role" and d["metadata"].get("namespace") == image_ns
             and all(d["metadata"]["labels"].get(k) == v for k, v in sel.items())]
    assert roles == [role]
    assert any(r["resources"] == ["virtualmachineimages"] and r.get("resourceNames") == ["image-abc"]
               for d in ci.docs(spec, V) if d["kind"] == "Role" and d["metadata"]["name"] == role
               and d["metadata"].get("namespace") == image_ns for r in d["rules"])


@pytest.mark.parametrize("path,same_ns,other", [
    ("docs/en/troubleshooting.md", "`caphv-identity` when the image is in the cluster namespace",
     "`caphv-identity-<cluster namespace>` in any other namespace (`<cluster namespace>-vms` included)"),
    ("docs/fr/depannage.md", "`caphv-identity` quand l'image est dans le namespace du cluster",
     "`caphv-identity-<namespace du cluster>` dans tout autre namespace (`<namespace du cluster>-vms` compris)"),
])
def test_the_troubleshooting_text_matches_the_code(path, same_ns, other):
    text = " ".join((ROOT / path).read_text().split())
    assert same_ns in text and other in text
    assert f"-l {ci.L_CLUSTER}=<cluster>,{ci.L_NAMESPACE}=" in text
    assert "patch role caphv-identity-" not in text                    # plus de nom écrit à la main
