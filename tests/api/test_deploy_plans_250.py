"""2.5.0 : format des phases capi et clusters, constats Cluster API de la
disponibilité (tâche 5).

* phases normalisées (délais par défaut, entrées, raccourci développé à la
  suite des entrées) ;
* un cas par refus (délai hors bornes, nom invalide, `count` à 0 et à 17,
  `name_pattern` sans `{{n}}`, `depends_on` vers une entrée placée après,
  variable non déclarée, plus de 16 entrées) ;
* plan écrit en 2.4.0 relu (phases désactivées à clés libres) : accepté,
  clés retirées à l'enregistrement, avertissement rendu et jamais écrit ;
* `deploy_chain.COMING` réduit à registry et artefacts, même message que
  `deploy_plans.COMING` ;
* disponibilité : chaque code Cluster API, plus « tout prêt » (lectures
  relevées sur harv1), codes absents si les phases sont désactivées ;
  règle du cœur : celle de capi_stack.stack_status (identité).

Mutations jouées (rapport des tâches 5 à 8) : M-coming (`capi` laissé dans
`COMING`), M-ready-copy (règle du cœur recopiée au lieu d'appeler
`stack_status`).
"""

import copy
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_stack              # noqa: E402
import deploy_chain as dc      # noqa: E402
import deploy_plans as dp      # noqa: E402
import deploy_ready as dr      # noqa: E402

FIX = json.loads((ROOT / "tests" / "fixtures" / "deploy" / "harv1-reads.json").read_text())
CAPI = {k: v for k, v in json.loads(
    (ROOT / "tests" / "fixtures" / "deploy" / "harv1-capi-reads.json").read_text()).items()
    if not k.startswith("_")}
# plan relevé tel qu'enregistré par la 2.4.0 (essai réel de sa tâche 6)
PLAN_240 = ROOT / "tests" / "fixtures" / "deploy" / "plan-240.yaml"


def plan(**phases):
    return {"variables": ["site"], "phases": phases}


def clusters(**over):
    ph = {"enabled": True, "items": [{"template": "rke2-petit", "name": "{{cluster}}-apps"}]}
    ph.update(over)
    return plan(capi={"enabled": True}, clusters=ph)


# --- format -------------------------------------------------------------------

def test_phases_normalized_with_defaults():
    p = dp.check_plan(clusters())
    assert p["phases"]["capi"] == {"enabled": True, "timeout_minutes": 10}
    assert p["phases"]["clusters"] == {"enabled": True, "timeout_minutes": 30,
                                       "items": [{"template": "rke2-petit", "name": "{{cluster}}-apps"}]}
    assert dp.enabled_phases(p) == ["capi", "clusters"]
    assert dp.plan_clusters(p) == p["phases"]["clusters"]["items"]


def test_shortcut_expanded_after_the_items():
    p = dp.check_plan(clusters(items=[{"template": "a", "name": "first"}],
                               template="rke2-petit", count=3, name_pattern="{{cluster}}-k8s-{{n}}",
                               timeout_minutes=45))
    ph = p["phases"]["clusters"]
    assert ph["timeout_minutes"] == 45
    assert [i["name"] for i in ph["items"]] == ["first", "{{cluster}}-k8s-1", "{{cluster}}-k8s-2",
                                               "{{cluster}}-k8s-3"]
    assert {i["template"] for i in ph["items"][1:]} == {"rke2-petit"}
    assert set(ph) == {"enabled", "timeout_minutes", "items"}     # le raccourci n'est pas gardé


def test_dependencies_kept_in_order():
    p = dp.check_plan(clusters(items=[{"template": "a", "name": "{{site}}-apps"},
                                      {"template": "a", "name": "{{site}}-tools",
                                       "depends_on": "{{site}}-apps"}]))
    assert p["phases"]["clusters"]["items"][1]["depends_on"] == ["{{site}}-apps"]


def test_clusters_without_capi_accepted():
    """Pile déjà présente : la vérification `capi-stack-missing` le dira."""
    p = dp.check_plan(plan(clusters={"items": [{"template": "a", "name": "c1"}]}))
    assert dp.enabled_phases(p) == ["clusters"]


@pytest.mark.parametrize("doc,where,why", [
    (plan(capi={"timeout_minutes": 0}), "phases.capi.timeout_minutes", "an integer from 1 to 60"),
    (plan(capi={"timeout_minutes": 61}), "phases.capi.timeout_minutes", "an integer from 1 to 60"),
    (plan(capi={"timeout_minutes": "10"}), "phases.capi.timeout_minutes", "an integer from 1 to 60"),
    (plan(capi={"caaph": False}), "phases.capi.caaph", "unknown key"),                 # décision 172
    (clusters(timeout_minutes=4), "phases.clusters.timeout_minutes", "an integer from 5 to 180"),
    (clusters(timeout_minutes=181), "phases.clusters.timeout_minutes", "an integer from 5 to 180"),
    (clusters(parallel=2), "phases.clusters.parallel", "unknown key"),                 # décision 170
    (clusters(items="a"), "phases.clusters.items", "not a list"),
    (clusters(items=[3]), "phases.clusters.items[0]", "not a mapping"),
    (clusters(items=[{"name": "a"}]), "phases.clusters.items[0].template", "missing or invalid template name"),
    (clusters(items=[{"template": "a", "name": "Apps"}]), "phases.clusters.items[0].name",
     "lowercase letters, digits and '-', starting with a letter, variables allowed"),
    (clusters(items=[{"template": "a", "name": "1apps"}]), "phases.clusters.items[0].name",
     "lowercase letters, digits and '-', starting with a letter, variables allowed"),
    (clusters(items=[{"template": "a", "name": "a" * 41}]), "phases.clusters.items[0].name",
     "lowercase letters, digits and '-', starting with a letter, variables allowed"),
    (clusters(items=[{"template": "a", "name": "{{equipe}}-apps"}]), "phases.clusters.items[0].name",
     "undeclared variable equipe"),
    (clusters(items=[{"template": "a", "name": "{{n}}-apps"}]), "phases.clusters.items[0].name",
     "undeclared variable n"),                                    # `n` : raccourci seulement
    (clusters(items=[{"template": "a", "name": "c", "namespace": "x"}]), "phases.clusters.items[0].namespace",
     "unknown key"),
    (clusters(items=[{"template": "a", "name": "c"}, {"template": "b", "name": "c"}]),
     "phases.clusters.items[1].name", "c listed twice"),
    (clusters(items=[{"template": "a", "name": "c", "depends_on": ["d"]}, {"template": "a", "name": "d"}]),
     "phases.clusters.items[0].depends_on", "d is not listed before this cluster"),
    (clusters(items=[{"template": "a", "name": "c", "depends_on": ["c"]}]),
     "phases.clusters.items[0].depends_on", "a cluster does not depend on itself"),
    (clusters(items=[{"template": "a", "name": "c", "depends_on": 3}]),
     "phases.clusters.items[0].depends_on", "not a list"),
    (clusters(items=[], template="a", count=0, name_pattern="k-{{n}}"), "phases.clusters.count",
     "an integer from 1 to 16"),
    (clusters(items=[], template="a", count=17, name_pattern="k-{{n}}"), "phases.clusters.count",
     "an integer from 1 to 16"),
    (clusters(items=[], template="a", count=2, name_pattern="k-x"), "phases.clusters.name_pattern",
     "a name holding {{n}}"),
    (clusters(items=[], count=2, name_pattern="k-{{n}}"), "phases.clusters.template",
     "missing or invalid template name"),
    (clusters(items=[{"template": "a", "name": f"c{i}"} for i in range(10)], template="a", count=7,
              name_pattern="k-{{n}}"), "phases.clusters.items", "more than 16 clusters"),
])
def test_refusals(doc, where, why):
    with pytest.raises(dp.ProfileError) as e:
        dp.check_plan(doc)
    assert (where, why) in e.value.errors, e.value.errors


@pytest.mark.parametrize("name,ok", [
    ("{{cluster}}-apps", True), ("apps-{{site}}", True), ("a", True), ("a" * 40, True),
    ("{{cluster}}", True), ("-apps", False), ("apps-", False), ("ap_ps", False), ("", False), (3, False)])
def test_name_shape(name, ok):
    assert dp.cluster_name_shape_ok(name) is ok


@pytest.mark.parametrize("name,values,out,why", [
    ("{{cluster}}-apps", {"cluster": "harvlab"}, "harvlab-apps", None),
    ("{{cluster}}-apps", {"cluster": "Harv1"}, None, "rendered name Harv1-apps is not valid"),
    ("{{cluster}}-apps", {"cluster": "c" * 36}, None, "rendered name " + "c" * 36 + "-apps is not valid"),
    ("{{site}}-x", {"cluster": "h"}, None, "missing variable site"),
])
def test_render_cluster_name(name, values, out, why):
    if why is None:
        assert dp.render_cluster_name(name, values) == out
    else:
        with pytest.raises(dp.ProfileError) as e:
            dp.render_cluster_name(name, values)
        assert why in e.value.errors[0][1]


def test_plan_written_by_240_is_read_and_cleaned(tmp_path):
    raw = yaml.safe_load(PLAN_240.read_text())
    assert raw["phases"]["capi"] == {"enabled": False, "providers": {"caaph": True}}
    p, warnings = dp.check_plan_warnings(raw)
    assert p["phases"]["capi"] == {"enabled": False, "timeout_minutes": 10}
    assert p["phases"]["clusters"] == {"enabled": False, "timeout_minutes": 30, "items": []}
    assert warnings == ["phases.capi.providers: unknown key, ignored (phase disabled)",
                        "phases.clusters.pools: unknown key, ignored (phase disabled)"]
    # lu sans avertissement (chargement), avertissement rendu à l'enregistrement, jamais écrit
    (tmp_path / "plans.d").mkdir(mode=0o700)
    (tmp_path / "plans.d" / "planbm.yaml").write_text(PLAN_240.read_text())
    assert dp.load_plan(tmp_path, "planbm")["phases"]["capi"]["timeout_minutes"] == 10
    out = dp.save_plan(tmp_path, "planbm", raw, revision=raw["revision"])
    assert out["warnings"] == warnings and out["revision"] == raw["revision"] + 1
    on_disk = yaml.safe_load((tmp_path / "plans.d" / "planbm.yaml").read_text())
    assert "warnings" not in on_disk and "providers" not in on_disk["phases"]["capi"]
    out = dp.save_plan(tmp_path, "planbm", on_disk, revision=on_disk["revision"])
    assert "warnings" not in out


def test_enabled_phase_from_240_is_strict():
    raw = yaml.safe_load(PLAN_240.read_text())
    raw["phases"]["capi"]["enabled"] = True
    with pytest.raises(dp.ProfileError) as e:
        dp.check_plan(raw)
    assert ("phases.capi.providers", "unknown key") in e.value.errors


def test_coming_reduced_to_registry_and_artefacts():
    """M-coming : `capi` laissé dans COMING fait échouer ce test."""
    assert dc.COMING == ("registry", "artefacts")
    assert dp.AVAILABLE == ("readiness", "terraform", "capi", "clusters")
    assert dc.COMING_MESSAGE == dp.COMING == "coming in 2.6+"
    assert not set(dc.COMING) & set(dp.AVAILABLE)
    assert set(dc.COMING) | set(dp.AVAILABLE) == set(dp.PHASES)


# --- disponibilité Cluster API ---------------------------------------------------

def reads(**over):
    r = copy.deepcopy(FIX)
    r.update(copy.deepcopy(CAPI))
    r.update(over)
    return r


def providers(core_phase="Ready", drop_core=False):
    out = copy.deepcopy(CAPI["capi_providers"])
    out = [p for p in out if not (drop_core and p["spec"]["type"] == "core")]
    for p in out:
        if p["spec"]["type"] == "core":
            p["status"]["phase"] = core_phase
    return out


def codes(ev):
    return [u["code"] for u in ev["unmet"]]


def test_all_ready_from_harv1():
    assert dr.evaluate(reads(), phases=["capi", "clusters"]) == {"ready": True, "unmet": []}


@pytest.mark.parametrize("over,code,detail", [
    ({"server_version": "v1.8.2"}, "harvester-too-old", "Harvester v1.8.2: Cluster API needs v1.9 or later"),
    ({"server_version": "v1.4.3"}, "harvester-too-old", "Harvester v1.4.3: Cluster API needs v1.9 or later"),
    ({"server_version": None}, "read-failed", "server-version"),
    ({"traefik": None}, "rke2-traefik-no-address",
     "kube-system/rke2-traefik has no address in status.loadBalancer.ingress"),
    ({"traefik": {"metadata": {"name": "rke2-traefik"}, "status": {"loadBalancer": {}}}},
     "rke2-traefik-no-address", "kube-system/rke2-traefik has no address in status.loadBalancer.ingress"),
    ({"capi_providers": None}, "turtles-not-ready", "no CAPIProvider readable"),
    ({"capi_providers": providers(core_phase="Provisioning")}, "turtles-not-ready",
     "the Cluster API core is not ready"),
    ({"capi_providers": providers(drop_core=True)}, "turtles-not-ready", "the Cluster API core is not ready"),
])
def test_each_capi_code(over, code, detail):
    ev = dr.evaluate(reads(**over), phases=["clusters"])
    assert not ev["ready"]
    assert {"code": code, "detail": detail} in ev["unmet"], ev


@pytest.mark.parametrize("version", ["v1.9.0", "v1.9.1-rc1", "v1.10.0", "v2.0.0"])
def test_recent_versions_pass(version):
    assert "harvester-too-old" not in codes(dr.evaluate(reads(server_version=version), phases=["capi"]))


def test_missing_providers_are_not_a_readiness_finding():
    """La phase capi déclare les fournisseurs : seule leur absence de cœur compte."""
    core_only = [p for p in providers() if p["spec"]["type"] == "core"]
    assert dr.evaluate(reads(capi_providers=core_only), phases=["capi"])["ready"]


@pytest.mark.parametrize("phases", [(), ["readiness", "terraform"]])
def test_capi_codes_absent_when_phases_disabled(phases):
    ev = dr.evaluate(reads(server_version="v1.8.2", traefik=None, capi_providers=None), phases=phases)
    assert ev == {"ready": True, "unmet": []}


def test_core_rule_is_the_one_of_stack_status(monkeypatch):
    """M-ready-copy : la règle du cœur vient de capi_stack.stack_status ;
    un changement de sa règle se voit ici (une copie ne le suivrait pas)."""
    seen = []
    real = capi_stack.stack_status

    def spy(kube, wanted=()):
        out = real(kube, wanted)
        seen.append(out)
        return dict(out, core=dict(out["core"] or {}, ready=False))
    monkeypatch.setattr(capi_stack, "stack_status", spy)
    ev = dr.evaluate(reads(), phases=["capi"])
    assert seen and codes(ev) == ["turtles-not-ready"]
    assert seen[0]["core"]["version"] == "v1.13.3" and seen[0]["turtles"] is True
