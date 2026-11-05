"""2.4.0 : plans de déploiement et modèles de déclaration (bin/lib/deploy_plans.py).

* contrôle des plans : un cas par refus (nom, phase inconnue, phase 2.5+
  activée, délai hors bornes, modèle absent, doublon, depends_on vers un
  modèle absent ou placé après, variable intégrée déclarée, plus de 32
  modèles, nom de déclaration trop long) ;
* rendu : variable absente avec son chemin, valeur à syntaxe YAML arrivée
  telle quelle, `{{vlan}}` seul sur `vlan_id` devenu entier, saut de ligne
  refusé ;
* révision (écriture périmée = PlanConflict), fichiers 0600, répertoire
  0700, aucun `.tmp` laissé ;
* modèles : mot de passe retiré et nommé, clé publique et nom de Secret
  gardés.

Mutations jouées (rapport de la tâche 2) : phase 2.5+ acceptée activée,
`scrub` sans exemption de `public_key`, rendu par substitution puis
relecture.
"""

import stat
import sys
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "web"))

import deploy_plans as dp     # noqa: E402
import tf_store as tfs        # noqa: E402

RID = ["a1a1a1a1a1a1", "b2b2b2b2b2b2", "c3c3c3c3c3c3"]


def plan_doc(**over):
    doc = {"description": "site", "variables": ["vlan", "reseau_vms"],
           "phases": {"readiness": {"enabled": True, "timeout_minutes": 20},
                      "terraform": {"enabled": True, "templates": [
                          {"template": "reseaux-site"},
                          {"template": "images-base", "depends_on": ["reseaux-site"]}]},
                      "capi": {"enabled": False, "providers": {"caaph": True}},
                      "clusters": {"enabled": False},
                      "registry": {"enabled": False},
                      "artefacts": {"enabled": False}}}
    doc.update(over)
    return doc


def with_phase(pid, value):
    doc = plan_doc()
    doc["phases"] = dict(doc["phases"], **{pid: value})
    return doc


def tf_templates(entries):
    return with_phase("terraform", {"enabled": True, "templates": entries})


def template_doc(**over):
    doc = {"description": "réseau du site", "variables": ["vlan", "reseau_vms"],
           "resources": [{"id": RID[0], "kind": "raw", "spec": {"tf": (
               'resource "harvester_network" "{{reseau_vms}}" {\n'
               '  name = "{{reseau_vms}}"\n  vlan_id = {{vlan}}\n}\n')}},
               {"id": RID[1], "kind": "ssh_key", "spec": {
                   "name": "{{cluster}}-ops", "public_key": "ssh-ed25519 AAAA ops"}}]}
    doc.update(over)
    return doc


# --- contrôle des plans -------------------------------------------------------

def test_plan_normalized():
    p = dp.check_plan(plan_doc())
    assert p["variables"] == ["vlan", "reseau_vms"]
    assert p["phases"]["readiness"] == {"enabled": True, "timeout_minutes": 20}
    assert p["phases"]["terraform"]["templates"] == [
        {"template": "reseaux-site"}, {"template": "images-base", "depends_on": ["reseaux-site"]}]
    # format de 2.5+ gardé tel quel, désactivé
    assert p["phases"]["capi"] == {"enabled": False, "providers": {"caaph": True}}
    assert dp.enabled_phases(p) == ["readiness", "terraform"]


def test_plan_defaults_and_shorthands():
    p = dp.check_plan({"phases": {"readiness": None, "terraform": {"templates": ["a", "b"]}}})
    assert p["phases"]["readiness"] == {"enabled": True, "timeout_minutes": dp.TIMEOUT_DEFAULT}
    assert p["phases"]["terraform"]["templates"] == [{"template": "a"}, {"template": "b"}]
    assert "capi" not in p["phases"]          # absente : n'émettra rien


@pytest.mark.parametrize("doc,where,why", [
    (plan_doc(name="Site"), "name", "invalid name (RFC 1123)"),
    ("not a plan", "plan", "not a mapping"),
    (plan_doc(extra=1), "extra", "unknown key"),
    (plan_doc(description="x" * 501), "description", "invalid"),
    (plan_doc(phases=[]), "phases", "not a mapping"),
    (with_phase("gitops", {"enabled": False}), "phases.gitops", "unknown phase"),
    (with_phase("capi", {"enabled": True}), "phases.capi", "coming in 2.5+"),
    (with_phase("capi", {"providers": {}}), "phases.capi", "coming in 2.5+"),   # enabled par défaut
    (with_phase("artefacts", {"enabled": True}), "phases.artefacts", "coming in 2.5+"),
    (with_phase("clusters", [{"template": "x"}]), "phases.clusters", "not a mapping"),
    (with_phase("readiness", {"enabled": "yes"}), "phases.readiness.enabled", "not a boolean"),
    (with_phase("readiness", {"timeout_minutes": 0}), "phases.readiness.timeout_minutes", "an integer from 1 to 180"),
    (with_phase("readiness", {"timeout_minutes": 181}), "phases.readiness.timeout_minutes", "an integer from 1 to 180"),
    (with_phase("readiness", {"timeout_minutes": True}), "phases.readiness.timeout_minutes", "an integer from 1 to 180"),
    (with_phase("readiness", {"timeout_minutes": "20"}), "phases.readiness.timeout_minutes", "an integer from 1 to 180"),
    (with_phase("readiness", {"delay": 3}), "phases.readiness.delay", "unknown key"),
    (with_phase("terraform", {"templates": "a"}), "phases.terraform.templates", "not a list"),
    (with_phase("terraform", {"order": []}), "phases.terraform.order", "unknown key"),
    (tf_templates([{"depends_on": []}]), "phases.terraform.templates[0].template", "missing or invalid template name"),
    (tf_templates([{"template": "Bad"}]), "phases.terraform.templates[0].template", "missing or invalid template name"),
    (tf_templates([3]), "phases.terraform.templates[0]", "not a mapping"),
    (tf_templates([{"template": "a", "when": 1}]), "phases.terraform.templates[0].when", "unknown key"),
    (tf_templates(["a", "a"]), "phases.terraform.templates[1].template", "a listed twice"),
    (tf_templates([{"template": "a", "depends_on": ["zz"]}]), "phases.terraform.templates[0].depends_on",
     "zz is not listed before this template"),
    (tf_templates([{"template": "a", "depends_on": ["b"]}, "b"]), "phases.terraform.templates[0].depends_on",
     "b is not listed before this template"),
    (tf_templates([{"template": "a", "depends_on": ["a"]}]), "phases.terraform.templates[0].depends_on",
     "a template does not depend on itself"),
    (tf_templates([{"template": "a", "depends_on": 3}]), "phases.terraform.templates[0].depends_on", "not a list"),
    (tf_templates([f"t{i}" for i in range(33)]), "phases.terraform.templates", "more than 32 templates"),
    (plan_doc(variables=["cluster"]), "variables[cluster]", "reserved name"),
    (plan_doc(variables=["vip"]), "variables[vip]", "reserved name"),
    (plan_doc(variables=["Bad"]), "variables[Bad]", "invalid name"),
    (plan_doc(variables=3), "variables", "not a list"),
    (dict(tf_templates(["t" * 30]), name="p" * 33), "phases.terraform.templates[0].template",
     "declaration name pppppppppppppppppppp... longer than 63 characters"),
])
def test_check_plan_refusals(doc, where, why):
    with pytest.raises(dp.ProfileError) as e:
        dp.check_plan(doc)
    assert (where, why) in e.value.errors


def test_check_plan_known_templates():
    with pytest.raises(dp.ProfileError) as e:
        dp.check_plan(plan_doc(), known_templates={"reseaux-site"})
    assert e.value.errors == [("phases.terraform.templates[1].template", "template images-base not found")]
    assert dp.check_plan(plan_doc(), known_templates={"reseaux-site", "images-base"})


def test_32_templates_accepted():
    assert len(dp.check_plan(tf_templates([f"t{i}" for i in range(32)]))["phases"]["terraform"]["templates"]) == 32


@pytest.mark.parametrize("plan,template,ok", [
    ("p" * 31, "t" * 31, True),                 # 63
    ("p" * 32, "t" * 31, False),                # 64
])
def test_declaration_name(plan, template, ok):
    if ok:
        assert dp.declaration_name(plan, template) == f"{plan}-{template}"
    else:
        with pytest.raises(dp.ProfileError):
            dp.declaration_name(plan, template)


# --- magasin --------------------------------------------------------------------

def test_plan_store_private_revisioned(tmp_path):
    state = tmp_path / "state"
    p = dp.save_plan(state, "site-standard", plan_doc())
    assert p["revision"] == 1 and p["name"] == "site-standard"
    path = state / "plans.d" / "site-standard.yaml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert str(tmp_path) not in path.read_text()
    assert dp.save_plan(state, "site-standard", plan_doc(description="v2"), 1)["revision"] == 2
    assert dp.load_plan(state, "site-standard")["description"] == "v2"
    assert [x["name"] for x in dp.list_plans(state)] == ["site-standard"]
    assert dp.list_plans(state)[0]["phases_enabled"] == ["readiness", "terraform"]
    assert not list(path.parent.glob("*.tmp")) and not list(path.parent.glob(".*.tmp"))
    assert dp.delete_plan(state, "site-standard") and dp.load_plan(state, "site-standard") is None
    assert not dp.delete_plan(state, "site-standard")


@pytest.mark.parametrize("existing,revision,conflict_with", [
    (False, 3, 0),            # création avec une révision : quelqu'un a supprimé
    (True, None, 1),          # écriture sans révision lue
    (True, 0, 1),
    (True, 2, 1),             # révision périmée (avance)
])
def test_plan_conflict(tmp_path, existing, revision, conflict_with):
    if existing:
        dp.save_plan(tmp_path, "p", plan_doc())
    with pytest.raises(dp.PlanConflict) as e:
        dp.save_plan(tmp_path, "p", plan_doc(description="later"), revision)
    assert e.value.current_revision == conflict_with
    if existing:
        assert dp.load_plan(tmp_path, "p")["description"] == "site"


def test_plan_stale_after_concurrent_write(tmp_path):
    dp.save_plan(tmp_path, "p", plan_doc())
    dp.save_plan(tmp_path, "p", plan_doc(description="a"), 1)
    with pytest.raises(dp.PlanConflict) as e:
        dp.save_plan(tmp_path, "p", plan_doc(description="b"), 1)
    assert e.value.current_revision == 2


def test_concurrent_writers_one_wins(tmp_path):
    dp.save_plan(tmp_path, "p", plan_doc())
    results = []

    def writer(i):
        try:
            dp.save_plan(tmp_path, "p", plan_doc(description=f"w{i}"), 1)
            results.append("ok")
        except dp.PlanConflict:
            results.append("conflict")
    ts = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == ["conflict"] * 5 + ["ok"]


@pytest.mark.parametrize("name,doc,where", [
    ("../x", plan_doc(), "name"),
    ("Site", plan_doc(), "name"),
    ("site", plan_doc(name="other"), "name"),
])
def test_save_plan_name_refusals(tmp_path, name, doc, where):
    with pytest.raises(dp.ProfileError) as e:
        dp.save_plan(tmp_path, name, doc)
    assert e.value.errors[0][0] == where
    assert not (tmp_path / "plans.d").exists() or not list((tmp_path / "plans.d").glob("*.yaml"))


def test_unreadable_plan_listed_invalid(tmp_path):
    d = tmp_path / "plans.d"
    d.mkdir()
    (d / "broken.yaml").write_text("phases: {capi: {enabled: true}}\n")
    (d / "garbage.yaml").write_text(": : :\n")
    assert dp.list_plans(tmp_path) == [{"name": "broken", "invalid": True},
                                       {"name": "garbage", "invalid": True}]
    assert dp.load_plan(tmp_path, "broken") is None


# --- modèles ----------------------------------------------------------------------

def test_template_round_trip(tmp_path):
    t, removed = dp.save_template(tmp_path, "reseaux-site", template_doc())
    assert removed == [] and t["revision"] == 1
    back = dp.load_template(tmp_path, "reseaux-site")
    assert back["resources"][1]["spec"]["public_key"] == "ssh-ed25519 AAAA ops"
    assert back["removed"] == []
    [item] = dp.list_templates(tmp_path)
    assert item["kinds"] == ["raw", "ssh_key"] and item["uses"] == ["cluster", "reseau_vms", "vlan"]
    path = tmp_path / "tf-templates.d" / "reseaux-site.yaml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert dp.delete_template(tmp_path, "reseaux-site")


def test_template_missing_ids_assigned():
    t, _ = dp.check_template({"resources": [{"kind": "ssh_key", "spec": {"name": "k", "public_key": "ssh-rsa A"}}]})
    assert tfs.ID_RE.match(t["resources"][0]["id"])


VM = {"name": "vm1", "cloudinit": [{"user_data": "#cloud-config\npassword: hunter2\nchpasswd: {expire: false}\n",
                                    "user_data_secret_name": "vm1-ci", "network_data": "version: 2\n"}],
      "ssh_keys": ["default/ops"], "admin_token": "abc", "api_key": "", "nested": {"private_key": "PEM"}}


def test_template_scrub():
    t, removed = dp.check_template({"resources": [{"id": RID[0], "kind": "vm", "spec": VM},
                                                  {"id": RID[1], "kind": "ssh_key",
                                                   "spec": {"name": "k", "public_key": "ssh-rsa A"}},
                                                  {"id": RID[2], "kind": "raw",
                                                   "spec": {"tf": 'resource "x" "y" { password = "p" }'}}]})
    assert removed == ["resources[0].spec.cloudinit[0].user_data", "resources[0].spec.admin_token",
                       "resources[0].spec.nested.private_key", "resources[2].spec.tf"]
    spec = t["resources"][0]["spec"]
    ci = spec["cloudinit"][0]
    assert "user_data" not in ci and ci["user_data_secret_name"] == "vm1-ci" and ci["network_data"]
    assert spec["ssh_keys"] == ["default/ops"] and spec["api_key"] == ""      # vide : rien à retirer
    assert t["resources"][1]["spec"]["public_key"] == "ssh-rsa A"
    assert "tf" not in t["resources"][2]["spec"]
    text = yaml.safe_dump(t)
    assert "hunter2" not in text and "PEM" not in text and "abc" not in text


def test_user_data_without_password_kept():
    ud = "#cloud-config\npackages: [qemu-guest-agent]\n"
    t, removed = dp.check_template({"resources": [{"id": RID[0], "kind": "vm",
                                                   "spec": {"name": "v", "cloudinit": {"user_data": ud}}}]})
    assert removed == [] and t["resources"][0]["spec"]["cloudinit"]["user_data"] == ud


def test_removed_paths_kept_in_file(tmp_path):
    _, removed = dp.save_template(tmp_path, "vms", {"resources": [{"id": RID[0], "kind": "vm", "spec": VM}]})
    assert dp.load_template(tmp_path, "vms")["removed"] == removed
    assert "hunter2" not in (tmp_path / "tf-templates.d" / "vms.yaml").read_text()


@pytest.mark.parametrize("doc,where,why", [
    ("x", "template", "not a mapping"),
    (template_doc(name="Bad"), "name", "invalid name (RFC 1123)"),
    (template_doc(other=1), "other", "unknown key"),
    (template_doc(resources={}), "resources", "not a list"),
    (template_doc(resources=[]), "resources", "no resource"),
    (template_doc(resources=[3]), "resources[0]", "not a mapping"),
    (template_doc(resources=[{"kind": "kvm_vm", "spec": {}}]), "resources",
     "resource #1: a kvm_vm resource does not go to a harvester target"),
    (template_doc(resources=[{"kind": "nope", "spec": {}}]), "resources", "resource #1: unknown kind 'nope'"),
    (template_doc(variables=[]), "{{vlan}}", "undeclared variable"),
    (template_doc(variables=["vlan", "reseau_vms", "vip"]), "variables[vip]", "reserved name"),
])
def test_check_template_refusals(doc, where, why):
    with pytest.raises(dp.ProfileError) as e:
        dp.check_template(doc)
    assert (where, why) in e.value.errors


def test_template_conflict(tmp_path):
    dp.save_template(tmp_path, "t", template_doc())
    with pytest.raises(dp.PlanConflict):
        dp.save_template(tmp_path, "t", template_doc(), revision=None)


# --- rendu --------------------------------------------------------------------------

def _templates():
    net = {"name": "reseaux-site", "resources": [
        {"id": RID[0], "kind": "raw", "spec": {"name": "{{reseau_vms}}", "vlan_id": "{{vlan}}",
                                               "description": "{{note}}", "labels": {"site": "{{cluster}}"}}}]}
    img = {"name": "images-base", "resources": [
        {"id": RID[1], "kind": "image", "spec": {"name": "cirros", "url": "http://{{vip}}/cirros.img"}}]}
    return {"reseaux-site": net, "images-base": img}


def _ft(kind, path):
    return {"vlan_id": "int"}.get(path)


VALUES = {"reseau_vms": "vms", "vlan": "42", "note": "a: b # c", "cluster": "harvlab", "vip": "172.16.2.60"}


def test_render_values_arrive_as_is():
    plan = dict(dp.check_plan(plan_doc()), name="site-standard")
    out = dp.render_templates(plan, _templates(), VALUES, _ft)
    assert [t for t, _ in out] == ["reseaux-site", "images-base"]
    spec = out[0][1][0]["spec"]
    assert spec["vlan_id"] == 42                        # entier : type du schéma
    assert spec["description"] == "a: b # c"            # syntaxe YAML gardée telle quelle
    assert spec["labels"] == {"site": "harvlab"}
    assert out[1][1][0]["spec"]["url"] == "http://172.16.2.60/cirros.img"
    assert out[0][1][0]["id"] == RID[0] and out[0][1][0]["kind"] == "raw"


def test_render_without_field_type_keeps_strings():
    plan = dict(dp.check_plan(plan_doc()), name="p")
    assert dp.render_templates(plan, _templates(), VALUES)[0][1][0]["spec"]["vlan_id"] == "42"


@pytest.mark.parametrize("values,templates,where,why", [
    ({k: v for k, v in VALUES.items() if k != "vlan"}, None,
     "templates[reseaux-site].resources[0].spec.vlan_id", "missing variable"),
    (dict(VALUES, vlan=""), None, "templates[reseaux-site].resources[0].spec.vlan_id", "missing variable"),
    ({k: v for k, v in VALUES.items() if k != "cluster"}, None,
     "templates[reseaux-site].resources[0].spec.labels.site", "missing variable"),
    (dict(VALUES, note="a\nb"), None, "variables.note", "control character or line break"),
    (dict(VALUES, note="x" * 513), None, "variables.note", "too long"),
    (dict(VALUES, note=3), None, "variables.note", "not a string"),
    (VALUES, {"reseaux-site": _templates()["reseaux-site"]}, "templates[images-base]", "template not found"),
])
def test_render_refusals(values, templates, where, why):
    plan = dict(dp.check_plan(plan_doc()), name="site-standard")
    with pytest.raises(dp.ProfileError) as e:
        dp.render_templates(plan, templates or _templates(), values, _ft)
    assert (where, why) in e.value.errors


def test_render_refuses_long_declaration_name():
    plan = dict(dp.check_plan(tf_templates(["t" * 31])), name="p" * 32)
    with pytest.raises(dp.ProfileError):
        dp.render_templates(plan, {"t" * 31: {"resources": []}}, {})


def test_used_variables():
    plan = dp.check_plan(plan_doc())
    assert dp.used_variables(plan, _templates()) == ["cluster", "note", "reseau_vms", "vip", "vlan"]
    assert dp.used_variables(plan, {}) == []
