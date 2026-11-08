"""2.7.0 (tâche 4) : format de la phase Artefacts d'un plan.

Contrôle statique (sans les paquets) puis, à l'enregistrement, avec la sorte
des éléments lue dans les paquets importés (`item_kind`). Variables du plan
permises dans les clusters et le registre, jamais dans le paquet ni
l'élément. Usage des registres et des paquets (suppression refusée).
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import deploy_plans as dp  # noqa: E402

KINDS = {("socle", "2026.11", "podinfo"): "chart", ("socle", "2026.11", "regles"): "manifest",
         ("socle", "2026.11", "net"): "policy", ("socle", "2026.11", "imgs"): "images",
         ("socle", "2026.11", "flux"): "gitops"}


def kind(pkg, ver, item):
    return KINDS.get((pkg, ver, item))


def doc(items=None, policies=None, clusters=("{{cluster}}-apps",), variables=("registre",), enabled=True, **ph):
    phases = {"clusters": {"enabled": True, "items": [{"template": "t", "name": n} for n in clusters]},
              "artefacts": dict({"enabled": enabled, "items": items or [], "policies": policies or []}, **ph)}
    return {"variables": list(variables), "phases": phases}


CHART = {"package": "socle", "version": "2026.11", "item": "podinfo", "registry": "{{registre}}",
         "clusters": ["{{cluster}}-apps"]}
MAN = {"package": "socle", "version": "2026.11", "item": "regles", "clusters": ["{{cluster}}-apps", "outils/outils"]}


def errors_of(d, item_kind=kind):
    with pytest.raises(dp.ProfileError) as e:
        dp.check_plan(d, item_kind=item_kind)
    return e.value.errors


@pytest.mark.parametrize("d,where,why", [
    (doc([dict(CHART, registry=None)]), "phases.artefacts.items[0].registry", "required for a chart"),
    (doc([dict(MAN, values={"a": 1})]), "phases.artefacts.items[0].values", "only for a chart, not a manifest"),
    (doc([dict(MAN, registry="amorcage")]), "phases.artefacts.items[0].registry", "only for a chart"),
    (doc([dict(MAN, item="imgs")]), "phases.artefacts.items[0].item", "published by the Registry phase"),
    (doc([dict(MAN, item="flux")]), "phases.artefacts.items[0].item", "applied by a GitOps entry"),
    (doc([dict(MAN, clusters=["inconnu"])]), "phases.artefacts.items[0].clusters[0]", "artefact-cluster-unknown"),
    (doc([dict(MAN, clusters=["A/b"])]), "phases.artefacts.items[0].clusters[0]", "artefact-cluster-unknown"),
    (doc([dict(MAN, clusters=["{{autre}}/x"])]), "phases.artefacts.items[0].clusters[0]", "undeclared variable"),
    (doc([dict(MAN, clusters=[])]), "phases.artefacts.items[0].clusters", "one at least"),
    (doc([dict(CHART, values={"password": "x"})]), "phases.artefacts.items[0].values.password",
     dp.SECRET_REFUSED),
    (doc([dict(CHART, values={"db": {"apiKey": "k"}})]), "phases.artefacts.items[0].values.db.apiKey",
     dp.SECRET_REFUSED),
    (doc([dict(CHART, values={"repo": "https://u:p@h/x"})]), "phases.artefacts.items[0].values.repo",
     "credentials in a URL"),
    (doc([dict(MAN, item="hops-policies")]), "phases.artefacts.items[0].item", "policies of the console"),
    (doc([MAN, dict(MAN)]), "phases.artefacts.items[1].item", "listed twice"),
    (doc([dict(MAN, depends_on=["podinfo"]), CHART]), "phases.artefacts.items[0].depends_on", "not listed before"),
    (doc([dict(MAN, package="Socle")]), "phases.artefacts.items[0].package", "a package name"),
    (doc([dict(MAN, version="{{v}}")]), "phases.artefacts.items[0].version", "x.y.z"),
    (doc([dict(CHART, registry="{{nope}}")]), "phases.artefacts.items[0].registry", "undeclared variable"),
    (doc([dict(MAN, extra=1)]), "phases.artefacts.items[0].extra", "unknown key"),
    (doc(timeout_minutes=61), "phases.artefacts.timeout_minutes", "an integer from 1 to 60"),
    (doc(policies=[{"clusters": ["{{cluster}}-apps"], "base": "oui"}]), "phases.artefacts.policies[0].base",
     "true or false"),
    (doc(policies=[{"clusters": ["{{cluster}}-apps"], "teams": [{"namespace": "kube-public"}]}]),
     "phases.artefacts.policies[0].teams[0].namespace", "kube-public is a system namespace"),
    (doc(policies=[{"clusters": ["{{cluster}}-apps"]}, {"clusters": ["{{cluster}}-apps"]}]),
     "phases.artefacts.policies[1].clusters", "already has its policies"),
])
def test_refusals(d, where, why):
    errs = errors_of(d)
    assert any(w == where and why in r for w, r in errs), errs


def test_limits_on_entries_and_unfolded_items():
    many = [dict(MAN, item=f"m{i}", clusters=["{{cluster}}-apps"]) for i in range(33)]
    assert any("32 entries at most" in r for _w, r in errors_of(doc(many), None))
    refs = [f"ns{i}/c{i}" for i in range(5)]
    wide = [dict(MAN, item=f"m{i}", clusters=refs) for i in range(26)]           # 130 éléments
    assert any("130 items once the clusters are unfolded" in r for _w, r in errors_of(doc(wide), None))
    ok = [dict(MAN, item=f"m{i}", clusters=refs[:4]) for i in range(32)]         # 128 : accepté
    assert len(dp.check_plan(doc(ok), item_kind=None)["phases"]["artefacts"]["items"]) == 32


def test_kind_rules_wait_for_the_package_without_a_resolver():
    out = dp.check_plan(doc([dict(CHART, registry=None), dict(MAN, item="imgs")]))
    assert [i["item"] for i in out["phases"]["artefacts"]["items"]] == ["podinfo", "imgs"]


def test_normalized_phase():
    out = dp.check_plan(doc([CHART, dict(MAN, depends_on=["podinfo"])],
                            policies=[{"clusters": ["{{cluster}}-apps"], "teams": [{"namespace": "equipe-a"}],
                                       "accounts": [{"namespace": "equipe-a", "name": "ci", "role": "edit"}]}]),
                        item_kind=kind)
    art = out["phases"]["artefacts"]
    assert art["timeout_minutes"] == 15 and art["enabled"] is True
    assert art["items"][1] == dict(MAN, depends_on=["podinfo"])
    # politiques gardées telles qu'écrites (les défauts ne sont pas figés dans le plan)
    assert art["policies"] == [{"clusters": ["{{cluster}}-apps"], "teams": [{"namespace": "equipe-a"}],
                                "accounts": [{"namespace": "equipe-a", "name": "ci", "role": "edit"}]}]


def test_a_2_6_plan_with_a_disabled_free_phase_loads_with_warnings(tmp_path):
    raw = {"phases": {"artefacts": {"enabled": False, "charts": ["x"], "items": [{"nope": 1}]}}}
    out, warnings = dp.check_plan_warnings(raw)
    assert out["phases"]["artefacts"]["enabled"] is False and out["phases"]["artefacts"]["items"] == []
    assert "phases.artefacts.charts: unknown key, ignored (phase disabled)" in warnings
    saved = dp.save_plan(tmp_path, "vieux", raw)
    assert saved["warnings"] and "charts" not in json.dumps(dp.load_plan(tmp_path, "vieux"))


def test_save_reads_the_kinds_from_the_imported_packages(tmp_path):
    d = tmp_path / "artefacts.d" / "socle-2026.11"
    d.mkdir(parents=True)
    (d / "artefacts.yaml").write_text(json.dumps({"name": "socle", "version": "2026.11", "items": [
        {"name": "podinfo", "kind": "chart"}, {"name": "regles", "kind": "manifest"}]}))
    with pytest.raises(dp.ProfileError) as e:
        dp.save_plan(tmp_path, "p", doc([dict(CHART, registry=None)]))
    assert ("phases.artefacts.items[0].registry", "required for a chart (published in this registry)") \
        in e.value.errors
    assert dp.save_plan(tmp_path, "p", doc([CHART, MAN]))["revision"] == 1


@pytest.mark.parametrize("names,values,rendered,expected", [
    (["{{cluster}}-apps"], {"cluster": "harvlab"}, {"{{cluster}}-apps": "harvlab-apps"},
     [("harvlab-apps/harvlab-apps", "harvlab-apps")]),
    (["outils/{{env}}"], {"env": "prod"}, {}, [("outils/prod", None)]),
])
def test_render_clusters(names, values, rendered, expected):
    assert dp.render_artefact_clusters({}, names, values, rendered) == expected


@pytest.mark.parametrize("names,values", [(["outils/{{env}}"], {}), (["outils/{{env}}"], {"env": "Prod_1"})])
def test_render_clusters_refusals(names, values):
    with pytest.raises(dp.ProfileError):
        dp.render_artefact_clusters({}, names, values, {})


def _write(tmp_path, name, d):
    p = tmp_path / "plans.d"
    p.mkdir(exist_ok=True)
    (p / f"{name}.yaml").write_text(json.dumps(d))


def test_registry_and_package_users_count_the_artefacts_phase(tmp_path):
    _write(tmp_path, "litteral", doc([dict(CHART, registry="amorcage")]))
    _write(tmp_path, "variable", doc([CHART]))
    _write(tmp_path, "autre", doc([MAN]))
    assert dp.registry_users(tmp_path, "amorcage") == [
        "plan:litteral", "plan:variable (possibly, through a variable)"]
    assert dp.package_users(tmp_path, "socle", "2026.11") == ["autre", "litteral", "variable"]
    assert dp.package_users(tmp_path, "socle", "2026.12") == []
