"""2.8.2 (décision 246) : sur un cluster Harvester existant, un plan dont la
phase Terraform crée l'image, la paire de clés ou le pool d'adresses qu'un
modèle de cluster désigne était refusé au lancement (`image-missing`,
`keypair-missing`, `pool-missing` bloquants ; seules les références écrites
avec des variables passaient en `refs-later`). Vu au banc 2.8.1 avec
`planbm281-small` sur harvlab2.

Ces constats deviennent `terraform-ref-later` (information) quand une
déclaration du plan crée la référence : lue dans les ressources rendues de
ses modèles (`image`, `ssh_key`, `ippool`, ou code libre à nom littéral),
jamais devinée. Le début de la phase Clusters rejoue `check --many` et
bloque si la référence manque encore.

Mutations jouées (rapport 2.8.2) : M1 ignorer le nom affiché d'une image,
M2 ignorer l'espace de noms (nom seul), M3 lire le code libre sans écarter
`count`, M4 prendre les ressources rendues d'une déclaration gardée telle
quelle, M5 lire les modèles d'une phase Terraform coupée.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import test_deploy_capi_250 as tc  # noqa: E402
import test_deploy_runner_240 as trn  # noqa: E402

env = tc.env
world = tc.world
capi = tc.capi

REF = "harvlab-apps/harvlab-apps"
URL = "https://download.opensuse.org/leap.qcow2"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKexampleexampleexampleexampleexampleexam ju@node1"


def image(name="leap", ns="default", display=None):
    spec = {"name": name, "namespace": ns, "url": URL}
    if display:
        spec["display_name"] = display
    return {"kind": "image", "spec": spec}


def ssh_key(name="{{equipe}}-key", ns="default"):
    return {"kind": "ssh_key", "spec": {"name": name, "namespace": ns, "public_key": KEY}}


def ippool(name="capi-vm-pool"):
    return {"kind": "ippool", "spec": {"name": name, "range": [{"start": "172.16.2.200", "end": "172.16.2.205",
                                                                 "subnet": "172.16.0.0/16",
                                                                 "gateway": "172.16.0.1"}]}}


def raw(body):
    return {"kind": "raw", "spec": {"tf": body}}


RAW_IMAGE = f'''resource "harvester_image" "leap" {{
  name         = "leap"
  namespace    = "default"
  display_name = "leap"
  source_type  = "download"
  url          = "{URL}"
}}
'''
RAW_COUNT = RAW_IMAGE.replace('  name         = "leap"\n', '  count        = 1\n  name         = "leap"\n')
RAW_LOCAL = RAW_IMAGE.replace('name         = "leap"', 'name         = "${local.n}"')
# relecture 2.8.2 I-2 : espace de noms non littéral, jamais pris pour `default`
RAW_NS_LOCAL = RAW_IMAGE.replace('namespace    = "default"', 'namespace    = local.ns')
RAW_NS_TMPL = RAW_IMAGE.replace('namespace    = "default"', 'namespace    = "def${local.x}"')
RAW_NO_NS = RAW_IMAGE.replace('  namespace    = "default"\n', '')
RAW_DISPLAY_LOCAL = RAW_IMAGE.replace('display_name = "leap"', 'display_name = local.d')


def seed(c, resources, terraform=True, ref=None):
    body = {"description": "refs", "variables": ["equipe"], "resources": resources}
    r = c.put("/api/baremetal/tf-templates/cles", json=body)
    assert r.status_code == 201, r.get_json()
    t = tc.template()
    t["spec"].update(ref or {})
    tc.seed(c, tc.plan(terraform=terraform), tmpl=t)


def run_check(w, code, fact, value, extra=None):
    w["capi"].fleet.append({"code": code, "level": "block", "facts": {fact: value}, "cluster": REF})
    body = {"plan": "site", "variables": {"equipe": "ops"}}
    body.update(extra or {})
    r = w["client"].post("/api/deploy/harvlab/check", json=body)
    assert r.status_code == 200, r.get_json()
    out = r.get_json()
    return {(f["code"], f["level"]) for f in out["findings"] if f["phase"] == "clusters"}, out


@pytest.mark.parametrize("case,resources,code,fact,value,later", [
    ("image-by-name", [image()], "image-missing", "image", "default/leap", True),
    ("image-by-display-name", [image("img-1", display="Leap 15.6")], "image-missing", "image",
     "default/Leap 15.6", True),                                                                     # M1
    ("image-other-namespace", [image(ns="equipe")], "image-missing", "image", "default/leap", False),  # M2
    ("image-other-name", [image("tumbleweed")], "image-missing", "image", "default/leap", False),
    ("keypair-rendered", [ssh_key()], "keypair-missing", "keypair", "default/ops-key", True),
    ("keypair-other-name", [ssh_key("autre")], "keypair-missing", "keypair", "default/ops-key", False),
    ("pool", [ippool()], "pool-missing", "pool", "capi-vm-pool", True),
    ("pool-other-name", [ippool("autre")], "pool-missing", "pool", "capi-vm-pool", False),
    ("pool-not-an-image", [ippool("leap")], "image-missing", "image", "default/leap", False),
    ("raw-literal", [raw(RAW_IMAGE)], "image-missing", "image", "default/leap", True),
    ("raw-count", [raw(RAW_COUNT)], "image-missing", "image", "default/leap", False),                # M3
    ("raw-not-literal", [raw(RAW_LOCAL)], "image-missing", "image", "default/leap", False),
    ("raw-namespace-not-literal", [raw(RAW_NS_LOCAL)], "image-missing", "image", "default/leap", False),   # I-2
    ("raw-namespace-template", [raw(RAW_NS_TMPL)], "image-missing", "image", "default/leap", False),       # I-2
    ("raw-namespace-unwritten", [raw(RAW_NO_NS)], "image-missing", "image", "default/leap", True),
    ("raw-display-not-literal", [raw(RAW_DISPLAY_LOCAL)], "image-missing", "image", "default/leap", True),
    ("three-resources-one-template", [ssh_key(), image(), ippool()], "pool-missing", "pool", "capi-vm-pool", True),
])
def test_refs_declared_by_the_terraform_phase_are_later(capi, case, resources, code, fact, value, later):
    seed(capi["client"], resources)
    got, out = run_check(capi, code, fact, value)
    if later:
        assert ("terraform-ref-later", "info") in got, out["findings"]
        assert (code, "blocking") not in got
        assert not out["blocking"], out["findings"]
        f = next(f for f in out["findings"] if f["code"] == "terraform-ref-later")
        assert f"check={code}" in f["detail"]
    else:
        assert (code, "blocking") in got, out["findings"]
        assert out["blocking"]


def test_terraform_phase_disabled_declares_nothing(capi):
    """M5 : le modèle existe mais la phase Terraform est coupée."""
    seed(capi["client"], [image()], terraform=False)
    got, out = run_check(capi, "image-missing", "image", "default/leap")
    assert ("image-missing", "blocking") in got and out["blocking"]


def _kept_declaration(w, resources):
    resources = [dict(r, id=f"{i:012x}") for i, r in enumerate(resources)]
    w["store"].create("aaaaaaaaaaaa", "harvlab", "site-cles", resources=resources, user="alice",
                      origin={"plan": "site", "template": "cles", "template_revision": 1, "plan_revision": 1})


@pytest.mark.parametrize("case,update,later", [
    ("kept-as-is", False, False),                                                                   # M4
    ("updated", True, True),
])
def test_an_existing_declaration_declares_what_it_will_apply(capi, case, update, later):
    """Déclaration de ce plan reprise, autres valeurs : gardée telle quelle
    sans `update_differing` (sa ressource à elle s'applique), mise à jour
    sinon (les ressources rendues s'appliquent)."""
    seed(capi["client"], [image()])
    _kept_declaration(capi, [image("tumbleweed")])
    got, out = run_check(capi, "image-missing", "image", "default/leap", {"update_differing": update})
    assert (("terraform-ref-later", "info") in got) is later, out["findings"]
    assert (("image-missing", "blocking") in got) is not later


def test_the_kept_declaration_satisfies_its_own_reference(capi):
    seed(capi["client"], [image("tumbleweed")])
    _kept_declaration(capi, [image()])
    got, out = run_check(capi, "image-missing", "image", "default/leap")
    assert ("terraform-ref-later", "info") in got and not out["blocking"], out["findings"]


def test_a_resource_that_does_not_render_declares_nothing():
    """Relecture 2.8.2 m-1 : une paire de clés sans clé publique ne se rend
    pas (la déclaration échoue) ; elle ne satisfait aucune référence."""
    bad = ssh_key("k")
    del bad["spec"]["public_key"]
    assert wapp._deploy_tf_declared({"a": [bad, ippool("p")]})["keypair"] == set()
    assert wapp._deploy_tf_declared({"a": [{"kind": "ippool", "spec": {"name": "p"}}]})["pool"] == set()


def test_declared_helper_reads_the_rendered_resources():
    got = wapp._deploy_tf_declared({"a": [image("img-1", display="Leap"), ssh_key("k", ns="equipe"), ippool("p"),
                                          raw(RAW_IMAGE), raw(RAW_COUNT), {"kind": "network", "spec": {"name": "n"}},
                                          "garbage", {"kind": "image", "spec": {"namespace": "x"}}],
                                    "b": None})
    assert got == {"image": {"default/img-1", "default/Leap", "default/leap"}, "keypair": {"equipe/k"},
                   "pool": {"p"}}


def test_still_missing_at_phase_start_blocks_before_any_creation(capi, monkeypatch):
    """Le contrôle passe (information) ; la déclaration ne crée pas l'image :
    `check --many` rejoué au début de la phase Clusters bloque, rien n'est
    créé."""
    fake = capi["capi"]
    seed(capi["client"], [image()])
    fake.fleet.append({"code": "image-missing", "level": "block", "facts": {"image": "default/leap"},
                       "cluster": REF})
    run = trn.start(capi)
    ph = tc.phase(run, "clusters")
    assert run.status == "error" and ph["message"] == "blocked by the pre-check: image-missing"
    assert fake.subs("create") == []


def test_created_by_terraform_then_the_cluster_is_made(capi, monkeypatch):
    fake = capi["capi"]
    seed(capi["client"], [image()])
    fake.fleet.append({"code": "image-missing", "level": "block", "facts": {"image": "default/leap"},
                       "cluster": REF})
    real = wapp._tf_decl_launch

    def tf_launch(*a, **kw):
        if a[3] == "apply":
            fake.fleet.clear()                       # la déclaration crée l'image
        return real(*a, **kw)
    monkeypatch.setattr(wapp, "_tf_decl_launch", tf_launch)
    run = trn.start(capi)
    assert run.status == "done", run.result
    assert len(fake.subs("create")) == 1


@pytest.mark.parametrize("case,text,want", [
    ("literal", 'resource "harvester_ippool" "p" {\n  name = "p"\n}\n', [("harvester_ippool", {"name": "p"})]),
    ("for-each", 'resource "harvester_ippool" "p" {\n  for_each = toset(["a"])\n  name = "p"\n}\n', []),
    ("template", 'resource "harvester_ippool" "p" {\n  name = "p-${var.x}"\n}\n',
     [("harvester_ippool", {"name": None})]),
    ("reference", 'resource "harvester_ippool" "p" {\n  name = "p"\n  description = local.d\n}\n',
     [("harvester_ippool", {"name": "p", "description": None})]),
    ("data-block", 'data "harvester_ippool" "p" {\n  name = "p"\n}\n', []),
    ("other-type", 'resource "harvester_network" "n" {\n  name = "n"\n}\n', []),
    ("unreadable", 'resource "harvester_ippool" "p" {\n  name = "p"\n', []),
])
def test_resource_literals(case, text, want):
    import tf_guard
    assert tf_guard.resource_literals(text, {"harvester_ippool"}) == want


@pytest.mark.parametrize("case,tf,want", [
    ("namespace-reference", RAW_NS_LOCAL, set()),
    ("display-reference", RAW_DISPLAY_LOCAL, {"default/leap"}),
    ("namespace-empty-form", None, {"default/leap"}),
    ("form-that-does-not-render", "no-url-no-name", set()),
    ("display-literal", RAW_IMAGE.replace('display_name = "leap"', 'display_name = "Leap"'),
     {"default/leap", "default/Leap"}),
])
def test_declared_free_code_never_guesses(case, tf, want):
    """Relecture 2.8.2 I-2 : un attribut écrit mais non littéral n'est pas
    remplacé par sa valeur par défaut."""
    res = image() if tf is None else {"kind": "image", "spec": {"url": URL}} if tf == "no-url-no-name" else raw(tf)
    if tf is None:
        res["spec"]["namespace"] = ""             # formulaire : vide = `default` au rendu
    assert wapp._deploy_tf_declared({"a": [res]})["image"] == want


def test_dry_run_passes_what_the_terraform_phase_would_make(capi):
    """Relecture 2.8.2 I-1 : en essai, la phase Terraform ne fait que
    planifier ; la référence qu'elle créerait manque encore au début de la
    phase Clusters. Ce constat reporté au lancement ne fait pas échouer
    l'essai : les clusters sont vérifiés au vrai déroulé, rien n'est créé."""
    fake = capi["capi"]
    seed(capi["client"], [image()])
    fake.fleet.append({"code": "image-missing", "level": "block", "facts": {"image": "default/leap"},
                       "cluster": REF})
    run = trn.start(capi, dry_run=True)
    ph = tc.phase(run, "clusters")
    assert run.status == "done", run.result
    assert ph["message"] == ("dry run: the clusters are checked once the earlier phases of the plan "
                             "have made what they name")
    assert ph["deferred"] == ["image-missing"] and fake.subs("create") == []


def test_dry_run_still_blocks_on_what_was_not_deferred(capi, monkeypatch):
    """Un constat bloquant apparu après le lancement (non reporté) arrête
    l'essai comme le vrai déroulé."""
    fake = capi["capi"]
    seed(capi["client"], [image()])
    fake.fleet.append({"code": "image-missing", "level": "block", "facts": {"image": "default/leap"},
                       "cluster": REF})
    real = wapp._tf_decl_launch

    def tf_launch(*a, **kw):
        fake.fleet.append({"code": "pool-missing", "level": "block", "facts": {"pool": "autre"}, "cluster": REF})
        return real(*a, **kw)
    monkeypatch.setattr(wapp, "_tf_decl_launch", tf_launch)
    run = trn.start(capi, dry_run=True)
    ph = tc.phase(run, "clusters")
    assert run.status == "error" and ph["message"] == "blocked by the pre-check: image-missing, pool-missing"
    assert fake.subs("create") == []


def test_dry_run_deferral_needs_the_same_facts(capi, monkeypatch):
    """Même code, autre référence (autre image) : non reporté, l'essai
    s'arrête."""
    fake = capi["capi"]
    seed(capi["client"], [image()])
    fake.fleet.append({"code": "image-missing", "level": "block", "facts": {"image": "default/leap"},
                       "cluster": REF})
    real = wapp._tf_decl_launch

    def tf_launch(*a, **kw):
        fake.fleet[0] = dict(fake.fleet[0], facts={"image": "default/autre"})
        return real(*a, **kw)
    monkeypatch.setattr(wapp, "_tf_decl_launch", tf_launch)
    run = trn.start(capi, dry_run=True)
    assert run.status == "error" and tc.phase(run, "clusters")["message"] == "blocked by the pre-check: image-missing"
