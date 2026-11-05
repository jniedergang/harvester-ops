"""2.4.0 : la ligne de commande bin/harvester-plans.py (tâche 7).

* plan et template : list, show, save (révision, refus), delete, en tableau
  sur un état temporaire, sans console ;
* check : constats, code 2 si l'un bloque (variable inconnue de --vars,
  cluster injoignable) ;
* apply : le déroulé de la console (Terraform simulé), étapes STEP_EVENT sur
  la sortie d'erreur, résultat JSON, codes 0 / 1 ;
* secrets : fichier 0644 refusé, valeur absente de stdout et stderr ;
* install avec une machine qui porte un système : code 2, ligne
  --confirm-system, rien de lancé ;
* analyse des arguments (abréviations refusées), --json à chaque sortie.

Mutation jouée (rapport des tâches 6-7) : lecture du fichier de secrets
sans contrôle de mode.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp  # noqa: E402
import test_deploy_routes_240 as tr  # noqa: E402
import test_deploy_runner_240 as trun  # noqa: E402

SPEC = importlib.util.spec_from_file_location("harvester_plans", ROOT / "bin" / "harvester-plans.py")
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)

env = tr.env
world = trun.world


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def write(p, doc, mode=0o600):
    p.write_text(yaml.safe_dump(doc))
    os.chmod(p, mode)
    return str(p)


# --- plans et modèles -----------------------------------------------------------

@pytest.mark.parametrize("what,body", [("plan", tr.plan_body()), ("template", tr.template_body())])
def test_store_commands(tmp_path, capsys, what, body):
    st = str(tmp_path / "st")
    f = write(tmp_path / "f.yaml", body)
    code, out, _ = run(capsys, "--state-dir", st, what, "save", "cles", "--file", f)
    assert code == 0 and json.loads(out)["revision"] == 1
    code, out, err = run(capsys, "--state-dir", st, what, "save", "cles", "--file", f)
    assert code == 2 and json.loads(out)["code"] == "conflict" and json.loads(out)["current_revision"] == 1
    code, out, _ = run(capsys, "--state-dir", st, what, "save", "cles", "--file", f, "--revision", "1")
    assert code == 0 and json.loads(out)["revision"] == 2
    code, out, _ = run(capsys, "--state-dir", st, what, "list")
    assert code == 0 and [x["name"] for x in json.loads(out)[f"{what}s"]] == ["cles"]
    code, out, _ = run(capsys, "--state-dir", st, what, "show", "cles")
    assert code == 0 and json.loads(out)["name"] == "cles"
    assert run(capsys, "--state-dir", st, what, "delete", "cles")[0] == 0
    code, out, _ = run(capsys, "--state-dir", st, what, "show", "cles")
    assert code == 2 and json.loads(out)["code"] == "not-found"


@pytest.mark.parametrize("argv,code", [
    (["plan", "show", "Bad"], "invalid-name"),
    (["plan", "save", "p", "--file", "/nonexistent.yaml"], "unreadable"),
])
def test_store_refusals(tmp_path, capsys, argv, code):
    rc, out, _ = run(capsys, "--state-dir", str(tmp_path), *argv)
    assert rc == 2 and json.loads(out)["code"] == code


def test_invalid_plan_lists_its_errors(tmp_path, capsys):
    f = write(tmp_path / "p.yaml", tr.plan_body(variables=["vip"]))
    rc, out, err = run(capsys, "--state-dir", str(tmp_path), "plan", "save", "p", "--file", f)
    assert rc == 2 and ["variables[vip]", "reserved name"] in json.loads(out)["errors"]
    assert "variables[vip]: reserved name" in err


@pytest.mark.parametrize("argv", [
    ["pl", "list"],                                   # abréviation refusée
    ["apply", "p", "--clus", "harvlab"],
    ["apply", "p"],                                   # --cluster requis
    ["resume", "a" * 12, "--secrets-file", "f", "--secrets-stdin"],
])
def test_argument_parsing(capsys, argv):
    rc, out, _ = run(capsys, "--json", *argv)
    assert rc == 2 and json.loads(out) == {"error": "invalid arguments", "code": "usage"}


# --- chaîne ----------------------------------------------------------------------

def _state(monkeypatch, e):
    monkeypatch.setattr(wapp, "_deploy_console_kc", lambda c: e["kc"] if c == "harvlab" else None)
    return ["--state-dir", str(e["state"])]


def test_check_findings_and_exit_codes(env, tmp_path, monkeypatch, capsys):
    tr.seed(env["client"])
    st = _state(monkeypatch, env)
    v = write(tmp_path / "v.yaml", {"equipe": "ops"})
    rc, out, err = run(capsys, *st, "check", "site", "--cluster", "harvlab", "--vars", v)
    assert rc == 0 and json.loads(out)["blocking"] is False and "readiness-later" in err
    v2 = write(tmp_path / "v2.yaml", {"equipe": "ops", "autre": "x"})
    rc, out, _ = run(capsys, *st, "check", "site", "--cluster", "harvlab", "--vars", v2)
    assert rc == 2 and "var-invalid" in [f["code"] for f in json.loads(out)["findings"]]
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda k, timeout=2.0: False)
    rc, out, _ = run(capsys, *st, "check", "site", "--cluster", "harvlab", "--vars", v)
    assert rc == 2 and "cluster-unreachable" in [f["code"] for f in json.loads(out)["findings"]]
    rc, out, _ = run(capsys, *st, "check", "site", "--cluster", "nope", "--vars", v)
    assert rc == 2 and json.loads(out)["code"] == "unknown-cluster"
    rc, out, _ = run(capsys, *st, "check", "nope", "--cluster", "harvlab")
    assert rc == 2 and json.loads(out)["code"] == "not-found"


def test_apply_runs_the_console_chain(world, tmp_path, monkeypatch, capsys):
    tr.seed(world["client"])
    st = _state(monkeypatch, world)
    v = write(tmp_path / "v.yaml", {"equipe": "ops"})
    rc, out, err = run(capsys, *st, "apply", "site", "--cluster", "harvlab", "--vars", v)
    d = json.loads(out)
    assert rc == 0 and d["status"] == "done" and d["result"]["plan"] == "site"
    assert "STEP_EVENT|tf-cles|done|applied" in err and "STEP_EVENT|phase-readiness|done|" in err
    world["fake"].actions["ops-ops"] = "delete"
    world["client"].put("/api/baremetal/tf-templates/cles", json=dict(tr.template_body(), revision=1))
    rc, out, err = run(capsys, *st, "apply", "site", "--cluster", "harvlab", "--vars", v, "--dry-run")
    assert rc == 1 and "plan-destroys" in err
    blocked = write(tmp_path / "b.yaml", {})
    rc, out, _ = run(capsys, *st, "apply", "site", "--cluster", "harvlab", "--vars", blocked)
    assert rc == 2 and json.loads(out)["code"] == "blocking-findings"


def test_resume_of_a_partial_run(world, tmp_path, monkeypatch, capsys):
    trun.two_templates(world)
    st = _state(monkeypatch, world)
    v = write(tmp_path / "v.yaml", {"equipe": "ops"})
    rc, out, _ = run(capsys, *st, "apply", "site", "--cluster", "harvlab", "--vars", v)
    first = json.loads(out)
    assert rc == 1 and first["result"]["status"] == "partial"
    world["fake"].fail.clear()
    rc, out, err = run(capsys, *st, "resume", first["action_id"])
    d = json.loads(out)
    assert rc == 0 and d["result"]["resumes"] == first["action_id"]
    assert "STEP_EVENT|tf-cles|skipped|already done (checked)" in err


# --- secrets ------------------------------------------------------------------

def test_secrets_file_readable_by_others_is_refused(world, tmp_path, monkeypatch, capsys):
    st = _state(monkeypatch, world)
    f = write(tmp_path / "s.yaml", {"token": trun.SECRET}, mode=0o644)
    nodes = tmp_path / "n.csv"
    nodes.write_text("bmc_host,bmc_user,hostname\n198.51.100.1,admin,node1\n")
    tr.seed(world["client"])
    tr.t180.create(world["client"])
    rc, out, err = run(capsys, *st, "install", "site", "--profile", "rack-a", "--nodes", str(nodes),
                       "--cluster-name", "rack-a", "--vip", "192.0.2.100", "--secrets-file", f)
    assert rc == 2 and json.loads(out)["code"] == "secrets" and "chmod 600" in err
    assert trun.SECRET not in out + err


@pytest.mark.real_presence
def test_install_with_a_present_machine_is_refused_nothing_started(world, tmp_path, monkeypatch, capsys):
    import test_bm_batch_presence_221 as t221
    monkeypatch.setattr(wapp, "_bm_system_presence", t221.fake_presence({"198.51.100.1": "present"}))
    tr.seed(world["client"])
    tr.t180.create(world["client"])
    st = _state(monkeypatch, world)
    nodes = tmp_path / "n.csv"
    nodes.write_text("bmc_host,bmc_user,hostname,ip,mgmt_mac,storage_ip,admin_ip\n"
                     "198.51.100.1,admin,node1,192.0.2.11,52:54:00:00:00:01,172.18.122.101,10.0.0.1\n")
    f = write(tmp_path / "s.yaml", {"token": trun.SECRET, "bmc_password": "BMCPW-cli"})
    with wapp.ACTIONS_LOCK:
        before = set(wapp.ACTIONS)
    rc, out, err = run(capsys, *st, "install", "site", "--profile", "rack-a", "--nodes", str(nodes),
                       "--cluster-name", "rack-a", "--vip", "192.0.2.100", "--secrets-file", f,
                       "--vars", write(tmp_path / "v.yaml", {"equipe": "ops"}))
    assert rc == 2 and json.loads(out)["code"] == "system-present"
    assert f"--confirm-system 198.51.100.1={t221.serial('198.51.100.1')}" in err
    with wapp.ACTIONS_LOCK:
        assert set(wapp.ACTIONS) == before
    assert trun.SECRET not in out + err and "BMCPW-cli" not in out + err


def test_install_from_stdin_runs_the_chain(world, tmp_path, monkeypatch, capsys):
    import io
    seen = []
    monkeypatch.setattr(wapp, "_bm_batch_launch", trun._fake_child("done", None, seen))
    monkeypatch.setattr(wapp, "_deploy_install_verify", lambda cluster, expected: (True, expected))
    tr.seed(world["client"], plan=tr.plan_body(readiness=False))
    tr.t180.create(world["client"])
    st = _state(monkeypatch, world)
    monkeypatch.setattr(wapp, "_deploy_console_kc", lambda c: world["kc"])
    nodes = tmp_path / "n.csv"
    nodes.write_text("bmc_host,bmc_user,hostname,ip,mgmt_mac,storage_ip,admin_ip\n"
                     "198.51.100.1,admin,node1,192.0.2.11,52:54:00:00:00:01,172.18.122.101,10.0.0.1\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO(yaml.safe_dump({"token": trun.SECRET, "bmc_password": "B"})))
    rc, out, err = run(capsys, *st, "install", "site", "--profile", "rack-a", "--nodes", str(nodes),
                       "--cluster-name", "rack-a", "--vip", "192.0.2.100", "--secrets-stdin",
                       "--vars", write(tmp_path / "v.yaml", {"equipe": "ops"}))
    d = json.loads(out)
    assert rc == 0, (out, err)
    assert d["result"]["install"]["hostnames"] == ["node1"] and seen[0][0]["token"] == trun.SECRET
    assert trun.SECRET not in out + err
    assert "STEP_EVENT|phase-install|done|" in err


def test_install_needs_secrets(world, tmp_path, capsys):
    rc, out, _ = run(capsys, "--state-dir", str(tmp_path), "install", "site", "--profile", "p", "--nodes", "n",
                     "--cluster-name", "c", "--vip", "v")
    assert rc == 2 and json.loads(out)["code"] == "usage"


def test_install_sh_links_the_command():
    import re
    text = (ROOT / "install.sh").read_text()
    assert re.search(r'ln -sf "\$PREFIX/harvester-plans\.py" "\$PREFIX/harvester-plans"', text)
    said = re.search(r'Scripts installed: harvester-\{([^}]*)\}', text).group(1).split(",")
    assert "plans" in said


@pytest.mark.parametrize("value,ok", [("ops", True), (12, True), (True, True),
                                      (None, False), ([1], False), ({"a": 1}, False)])
def test_vars_values_are_scalars_like_in_the_console(env, tmp_path, monkeypatch, capsys, value, ok):
    """Revue m6 : None, liste et dictionnaire refusés (la console les refuse
    en « not a string ») au lieu de devenir le texte "None"."""
    tr.seed(env["client"])
    st = _state(monkeypatch, env)
    v = write(tmp_path / "v.yaml", {"equipe": value})
    rc, out, _ = run(capsys, *st, "check", "site", "--cluster", "harvlab", "--vars", v)
    if ok:
        assert rc == 0, out
    else:
        assert rc == 2 and json.loads(out)["code"] == "invalid-vars"


def test_apply_update_declarations_flag(world, tmp_path, monkeypatch, capsys):
    """Revue B2-I6 : la ligne de commande demande la mise à jour d'une
    déclaration dont les valeurs diffèrent, comme la fenêtre."""
    tr.seed(world["client"])
    st = _state(monkeypatch, world)
    rc, _out, _err = run(capsys, *st, "apply", "site", "--cluster", "harvlab",
                         "--vars", write(tmp_path / "v.yaml", {"equipe": "ops"}))
    assert rc == 0
    v2 = write(tmp_path / "v2.yaml", {"equipe": "dev"})
    rc, out, err = run(capsys, *st, "check", "site", "--cluster", "harvlab", "--vars", v2)
    assert rc == 0 and "decl-differs" in err
    rc, out, err = run(capsys, *st, "apply", "site", "--cluster", "harvlab", "--vars", v2,
                       "--update-declarations")
    d = json.loads(out)
    assert rc == 0 and d["result"]["phases"][1]["items"][0]["updated"] is True
    assert world["store"].find_by_name("harvlab", "site-cles")["resources"][0]["spec"]["name"] == "dev-ops"
