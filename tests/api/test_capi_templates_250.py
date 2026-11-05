"""2.5.0 : modèles de cluster nommés (`capi-templates.d`), décision 173.

Un modèle porte la demande du formulaire de création Cluster API (liste
blanche de capi_cluster, moins nom et namespaces, que donnent l'entrée du
plan et la règle du namespace), chaînes à variables `{{nom}}`. Rangement,
révision, remplacement atomique : les fonctions communes de deploy_plans.
"""

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import deploy_plans as dp  # noqa: E402
import test_deploy_routes_240 as tr  # noqa: E402

env = tr.env

SPEC = importlib.util.spec_from_file_location("harvester_plans_250", ROOT / "bin" / "harvester-plans.py")
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


def body(**spec_over):
    spec = {"k8s_version": "v1.34.11", "cp_replicas": 1, "worker_replicas": "{{workers}}",
            "cpu": 2, "memory": "4Gi", "disk_size": "20Gi", "image": "{{image}}",
            "ssh_keypair": "default/ju", "network": "default/production", "gateway": "172.16.0.1",
            "subnet_mask": "255.255.0.0", "ip_pool": "capi-vm-pool", "dns": ["172.16.3.6"]}
    spec.update(spec_over)
    return {"description": "petit cluster", "variables": ["workers", "image"],
            "spec": {k: v for k, v in spec.items() if v is not None}}


# ---------------------------------------------------------------------------
# Contrôle
# ---------------------------------------------------------------------------

def test_a_valid_template_is_normalized():
    t = dp.check_capi_template(body())
    assert t["variables"] == ["workers", "image"]
    assert t["spec"]["worker_replicas"] == "{{workers}}" and t["spec"]["cpu"] == 2


@pytest.mark.parametrize("doc,where,why", [
    (body(owner="x"), "spec.owner", "unknown key"),                                   # clé inconnue
    (body(name="web"), "spec.name", "set by the plan, not by the template"),          # clés interdites
    (body(namespace="web"), "spec.namespace", "set by the plan, not by the template"),
    (body(target_namespace="default"), "spec.target_namespace", "set by the plan, not by the template"),
    (body(network="{{net}}"), "{{net}}", "undeclared variable"),                      # variable non déclarée
    (body(cpu=0), "spec.cpu", "between 1 and 64"),                                    # cpu à 0 sans variable
    (body(memory="4G"), "spec.memory", "a size such as 8Gi"),
    (body(fleet_repo="https://bob:pw@git.example/x.git"), "spec.fleet_repo", "credentials in a URL"),
    (body(fleet_repo="https://git.example/x.git?token=abc"), "spec.fleet_repo", "credentials in a URL"),
    (body(dns={"a": 1}), "spec.dns", "a string, a number, a boolean or a list of strings"),
    (dict(body(), extra=1), "extra", "unknown key"),
    (dict(body(), variables=["cluster"]), "variables[cluster]", "reserved name"),
])
def test_each_refusal(doc, where, why):
    with pytest.raises(dp.ProfileError) as e:
        dp.check_capi_template(doc)
    assert (where, why) in e.value.errors, e.value.errors


def test_an_unreadable_integer_is_refused():
    with pytest.raises(dp.ProfileError) as e:
        dp.check_capi_template(body(cpu="deux"))
    assert any(w == "spec" and "cpu" in r for w, r in e.value.errors)


def test_builtin_variables_need_no_declaration():
    t = dp.check_capi_template(dict(body(ssh_keypair="default/{{cluster}}-key"), variables=["workers", "image"]))
    assert t["spec"]["ssh_keypair"] == "default/{{cluster}}-key"


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------

def _t(**over):
    return dict(dp.check_capi_template(body(**over)), name="rke2-petit")


def test_a_lone_variable_on_an_integer_option_becomes_an_integer():
    spec = dp.render_capi_template(_t(cpu="{{workers}}"), {"workers": "3", "image": "default/img"}, "site1-apps")
    assert spec["worker_replicas"] == 3 and isinstance(spec["worker_replicas"], int)
    assert spec["cpu"] == 3 and isinstance(spec["cpu"], int)
    assert (spec["name"], spec["namespace"]) == ("site1-apps",) * 2
    # namespace des VMs laissé au défaut : harvester-capi l'impose (`site1-apps-vms`)
    assert spec["target_namespace"] == "default"
    import capi_identity as ci
    assert ci.force_target_namespace(spec, "restricted")["target_namespace"] == "site1-apps-vms"


def test_a_value_with_yaml_syntax_arrives_as_is():
    spec = dp.render_capi_template(_t(network="default/{{image}}"), {"workers": "1", "image": "a: b # c"}, "c1")
    assert spec["network"] == "default/a: b # c"
    assert spec["image"] == "a: b # c"


def test_a_missing_variable_is_named_with_its_path():
    with pytest.raises(dp.ProfileError) as e:
        dp.render_capi_template(_t(), {"workers": "1"}, "site1-apps")
    assert e.value.errors == [("clusters[site1-apps].template[rke2-petit].spec.image", "missing variable")]


@pytest.mark.parametrize("name,ok", [("a" * 40, True), ("a" * 41, False), ("1abc", False), ("Abc", False)])
def test_rendered_name_rules(name, ok):
    vals = {"workers": "1", "image": "default/img"}
    if ok:
        assert dp.render_capi_template(_t(), vals, name)["name"] == name
    else:
        with pytest.raises(dp.ProfileError) as e:
            dp.render_capi_template(_t(), vals, name)
        assert e.value.errors[0][0].endswith(".name")


@pytest.mark.parametrize("vals,where", [
    ({"workers": "1", "image": "x\ny"}, "variables.image"),
    ({"workers": "abc", "image": "default/img"}, "clusters[c1].template[rke2-petit].spec"),
])
def test_render_refusals(vals, where):
    with pytest.raises(dp.ProfileError) as e:
        dp.render_capi_template(_t(), vals, "c1")
    assert e.value.errors[0][0] == where


def test_a_variable_cannot_smuggle_credentials_into_a_url():
    t = dict(dp.check_capi_template(dict(body(fleet_repo="https://{{host}}/x.git"),
                                         variables=["workers", "image", "host"])), name="m")
    with pytest.raises(dp.ProfileError) as e:
        dp.render_capi_template(t, {"workers": "1", "image": "default/i", "host": "bob:pw@git.example"}, "c1")
    assert e.value.errors[0][1] == "credentials in a URL"


# ---------------------------------------------------------------------------
# Rangement
# ---------------------------------------------------------------------------

def test_store_revision_mode_and_listing(tmp_path):
    st = tmp_path / "st"
    out = dp.save_capi_template(st, "rke2-petit", body(), by="alice")
    assert out["revision"] == 1 and out["updated_by"] == "alice"
    p = dp.capi_template_path(st, "rke2-petit")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    with pytest.raises(dp.PlanConflict) as e:
        dp.save_capi_template(st, "rke2-petit", body())               # révision périmée (création)
    assert e.value.current_revision == 1
    assert dp.save_capi_template(st, "rke2-petit", body(), 1)["revision"] == 2
    lst = dp.list_capi_templates(st)
    assert lst == [{"name": "rke2-petit", "revision": 2, "description": "petit cluster",
                    "variables": ["workers", "image"], "uses": ["image", "workers"], "vms": None}]
    assert dp.load_capi_template(st, "rke2-petit")["spec"]["image"] == "{{image}}"


def _plan(st, name, template):
    d = st / dp.PLAN_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text(yaml.safe_dump({
        "name": name, "revision": 1, "phases": {"clusters": {"enabled": False, "items": [
            {"template": template, "name": "{{cluster}}-apps"}]}}}))


def test_plans_that_use_a_template(tmp_path):
    st = tmp_path / "st"
    _plan(st, "p1", "rke2-petit")
    _plan(st, "p2", "autre")
    (st / dp.PLAN_DIR / "p3.yaml").write_text(yaml.safe_dump(
        {"name": "p3", "phases": {"clusters": {"template": "rke2-petit", "count": 2}}}))
    assert dp.plans_using_capi_template(st, "rke2-petit") == ["p1", "p3"]
    assert dp.plans_using_capi_template(st, "rien") == []


# ---------------------------------------------------------------------------
# Routes (mêmes formes que tf-templates)
# ---------------------------------------------------------------------------

def test_routes(env):
    import app as wapp
    c = wapp.app.test_client()
    r = c.put("/api/baremetal/capi-templates/rke2-petit", json=body())
    assert r.status_code == 201 and r.get_json()["revision"] == 1
    r = c.put("/api/baremetal/capi-templates/rke2-petit", json=body())
    assert r.status_code == 409 and r.get_json()["current_revision"] == 1
    r = c.put("/api/baremetal/capi-templates/rke2-petit", json=dict(body(), revision=1))
    assert r.status_code == 200 and r.get_json()["revision"] == 2
    r = c.put("/api/baremetal/capi-templates/rke2-petit", json=body(target_namespace="x"))
    assert r.status_code == 400
    assert [x["name"] for x in c.get("/api/baremetal/capi-templates").get_json()["templates"]] == ["rke2-petit"]
    got = c.get("/api/baremetal/capi-templates/rke2-petit").get_json()
    assert got["spec"]["image"] == "{{image}}" and got["used_by"] == []
    _plan(Path(env["state"]), "p1", "rke2-petit")
    r = c.delete("/api/baremetal/capi-templates/rke2-petit")
    assert r.status_code == 409 and r.get_json() == {"error": "saved plans use this cluster template",
                                                      "code": "template-in-use", "plans": ["p1"]}
    (Path(env["state"]) / dp.PLAN_DIR / "p1.yaml").unlink()
    assert c.delete("/api/baremetal/capi-templates/rke2-petit").status_code == 204
    assert c.get("/api/baremetal/capi-templates/rke2-petit").status_code == 404
    assert c.get("/api/baremetal/capi-templates/Bad").status_code == 400


def test_writes_are_admin_only_and_limits_are_valid():
    import app as wapp
    from limits import parse_many
    assert any("/api/baremetal/capi-templates/x".startswith(p) for p in wapp.ADMIN_ONLY_PREFIXES)
    src = (ROOT / "web" / "app.py").read_text()
    i = src.index('def api_capi_template_put')
    assert '@_rate_limit("30/minute")' in src[i - 120:i]
    parse_many("30/minute")


# ---------------------------------------------------------------------------
# Ligne de commande
# ---------------------------------------------------------------------------

def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_capi_template_commands(tmp_path, capsys):
    st = str(tmp_path / "st")
    f = tmp_path / "m.yaml"
    f.write_text(yaml.safe_dump(body()))
    os.chmod(f, 0o600)
    code, out, _ = run(capsys, "--state-dir", st, "capi-template", "save", "rke2-petit", "--file", str(f))
    assert code == 0 and json.loads(out)["revision"] == 1 and json.loads(out)["updated_by"].startswith("cli:")
    code, out, _ = run(capsys, "--state-dir", st, "capi-template", "save", "rke2-petit", "--file", str(f))
    assert code == 2 and json.loads(out)["code"] == "conflict"
    code, out, _ = run(capsys, "--state-dir", st, "capi-template", "list")
    assert [x["name"] for x in json.loads(out)["cluster_templates"]] == ["rke2-petit"]
    code, out, _ = run(capsys, "--state-dir", st, "capi-template", "show", "rke2-petit")
    assert json.loads(out)["spec"]["worker_replicas"] == "{{workers}}"
    _plan(Path(st), "p1", "rke2-petit")
    code, out, _ = run(capsys, "--state-dir", st, "capi-template", "delete", "rke2-petit")
    assert code == 2 and json.loads(out)["code"] == "template-in-use" and json.loads(out)["plans"] == ["p1"]
    (Path(st) / dp.PLAN_DIR / "p1.yaml").unlink()
    assert run(capsys, "--state-dir", st, "capi-template", "delete", "rke2-petit")[0] == 0


def test_cli_invalid_cluster_template(tmp_path, capsys):
    f = tmp_path / "m.yaml"
    f.write_text(yaml.safe_dump(body(namespace="web")))
    code, out, err = run(capsys, "--state-dir", str(tmp_path), "capi-template", "save", "m", "--file", str(f))
    assert code == 2 and json.loads(out)["code"] == "invalid-cluster-template"
    assert ["spec.namespace", "set by the plan, not by the template"] in json.loads(out)["errors"]
