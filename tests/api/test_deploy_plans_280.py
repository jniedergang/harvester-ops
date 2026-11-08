"""2.8.0 (tâche 5) : phase GitOps du format des plans, options d'accès des
modèles de cluster, sortes rke2 et gitops refusées par la phase Artefacts.

Mutation jouée (rapport des tâches 5 à 8) : « dépendance au chart flux
ignorée » (test_deploy_gitops_280).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import deploy_chain as dch  # noqa: E402
import deploy_plans as dp  # noqa: E402

OCI = {"type": "oci", "package": "banc-280", "version": "1.0.0", "item": "apps", "registry": "amorcage"}
GIT = {"type": "git", "url": "ssh://git@git.client/infra.git", "branch": "main", "path": "./site", "secret": "git-infra"}


def entry(**over):
    e = {"name": "apps", "tool": "auto", "clusters": ["{{cluster}}-a"], "target_namespace": "equipe-a",
         "source": dict(OCI)}
    e.update(over)
    return e


def doc(entries, variables=("equipe",), enabled=True, extra=None):
    ph = {"enabled": enabled, "timeout_minutes": 15, "entries": list(entries)}
    ph.update(extra or {})
    return {"variables": list(variables),
            "phases": {"clusters": {"items": [{"template": "t", "name": "{{cluster}}-a"},
                                              {"template": "t", "name": "{{cluster}}-b"}]},
                       "gitops": ph}}


def errors(d):
    with pytest.raises(dp.ProfileError) as e:
        dp.check_plan(d)
    return e.value.errors


def test_the_format_of_the_plan_example_is_accepted():
    """Exemple du plan 2.8.0 (tâche 5), une source oci et une source git."""
    d = doc([entry(), entry(name="infra", clusters=["{{cluster}}-b"], target_namespace="equipe-b", source=dict(GIT))])
    got = dp.check_plan(d)["phases"]["gitops"]
    assert got["timeout_minutes"] == 15 and got["enabled"] is True
    apps, infra = got["entries"]
    assert apps == {"name": "apps", "tool": "auto", "privileges": "namespace", "prune": True, "interval_minutes": 10,
                    "source": OCI, "target_namespace": "equipe-a", "clusters": ["{{cluster}}-a"]}
    assert infra["source"] == GIT and infra["target_namespace"] == "equipe-b"
    # forme rendue relue telle quelle (enregistrement puis chargement)
    assert dp.check_plan(dict(d, phases=dict(d["phases"], gitops=got)))["phases"]["gitops"] == got
    assert dp.gitops_secret_names(dp.check_plan(d)) == ["git-infra"]


@pytest.mark.parametrize("e,where,why", [
    (entry(color="red"), "phases.gitops.entries[0].color", "unknown key"),
    (entry(name="a" * 35), "phases.gitops.entries[0].name", "34 characters at most"),
    (entry(name="Apps"), "phases.gitops.entries[0].name", "RFC 1123"),
    (entry(clusters=[]), "phases.gitops.entries[0].clusters", "one at least"),
    (entry(clusters=["inconnu"]), "phases.gitops.entries[0].clusters[0]", "gitops-cluster-unknown"),
    (entry(clusters=["ns/{{zone}}"]), "phases.gitops.entries[0].clusters[0]", "undeclared variable zone"),
    (entry(tool="argo"), "phases.gitops.entries[0].tool", "one of auto, flux, fleet"),
    (entry(privileges="root"), "phases.gitops.entries[0].privileges", "one of namespace, cluster"),
    (entry(target_namespace="Equipe"), "phases.gitops.entries[0].target_namespace", "team namespace"),
    (entry(interval_minutes=0), "phases.gitops.entries[0].interval_minutes", "1 to 1440"),
    (entry(source={"type": "s3"}), "phases.gitops.entries[0].source.type", "one of oci, git, chart"),
    (entry(source=dict(OCI, url="x")), "phases.gitops.entries[0].source.url", "unknown key for a oci source"),
    (entry(source=dict(OCI, registry="{{zone}}")), "phases.gitops.entries[0].source.registry",
     "undeclared variable zone"),
    (entry(source=dict(OCI, registry="Amorcage")), "phases.gitops.entries[0].source.registry", "a registry name"),
    (entry(source=dict(GIT, url="https://u:p@git.client/x.git")), "phases.gitops.entries[0].source.url",
     "without credentials"),
    (entry(source=dict(GIT, url="ftp://git.client/x.git")), "phases.gitops.entries[0].source.url",
     "without credentials"),
    (entry(source=dict(GIT, secret="Mon_Secret")), "phases.gitops.entries[0].source.secret", "secret of the plan"),
    (entry(source={"type": "chart", "package": "p", "version": "1", "item": "i"}),
     "phases.gitops.entries[0].source.registry", "required"),
])
def test_entry_refusals(e, where, why):
    got = errors(doc([e]))
    assert any(w == where and why in r for w, r in got), got


def test_an_entry_listed_twice_is_refused():
    got = errors(doc([entry(), entry(clusters=["{{cluster}}-b"])]))
    assert ("phases.gitops.entries[1].name", "apps listed twice") in got


def test_sixteen_entries_at_most():
    many = [entry(name=f"e{i}", clusters=[f"ns/c{i}"]) for i in range(17)]
    assert ("phases.gitops.entries", "16 entries at most") in errors(doc(many))
    assert len(dp.check_plan(doc(many[:16]))["phases"]["gitops"]["entries"]) == 16


def test_sixty_four_items_once_the_clusters_are_unfolded():
    clusters = [f"ns/c{i}" for i in range(13)]
    five = [entry(name=f"e{i}", clusters=clusters) for i in range(5)]         # 65 éléments
    assert ("phases.gitops.entries", "65 items once the clusters are unfolded, 64 at most") in errors(doc(five))
    clusters = clusters[:12] + ["ns/c99", "ns/c98", "ns/c97", "ns/c96"]
    four = [entry(name=f"e{i}", clusters=clusters) for i in range(4)]         # 64
    assert len(dp.check_plan(doc(four))["phases"]["gitops"]["entries"]) == 4


@pytest.mark.parametrize("key,bad", [("timeout_minutes", 0), ("timeout_minutes", 61)])
def test_timeout_bounds(key, bad):
    assert any(w == "phases.gitops.timeout_minutes" for w, _r in errors(doc([entry()], extra={key: bad})))


def test_a_disabled_phase_with_free_keys_is_kept_with_warnings():
    """Plan écrit avant que la phase ne soit figée (2.7) : clés ignorées."""
    d = doc([entry(color="red")], enabled=False, extra={"notes": "plus tard"})
    out, warnings = dp.check_plan_warnings(d)
    assert out["phases"]["gitops"] == {"enabled": False, "timeout_minutes": 15, "entries": []}
    assert "phases.gitops.notes: unknown key, ignored (phase disabled)" in warnings
    assert "phases.gitops.entries[0].color: unknown key, ignored (phase disabled)" in warnings
    assert dp.gitops_secret_names(out) == []


def test_gitops_is_the_last_phase_of_the_chain():
    assert dp.PHASES[-1] == "gitops" and dch.ORDER[-1] == "gitops"
    assert dch.ORDER.index("artefacts") < dch.ORDER.index("gitops")


@pytest.mark.parametrize("kind,why", [("gitops", "applied by a GitOps entry"),
                                      ("rke2", "published by the Registry phase"),
                                      ("images", "published by the Registry phase")])
def test_artefacts_refuse_gitops_and_rke2_items(kind, why):
    got = dp.artefact_entry_refusals({"package": "p", "version": "1.0.0", "item": "i"}, kind, "w")
    assert got == [("w.item", f"a {kind} item is not applied: {why}")]


def test_registry_and_package_users_see_the_gitops_sources(tmp_path):
    d = doc([entry()])
    dp.save_plan(tmp_path, "site", d)
    assert dp.registry_users(tmp_path, "amorcage") == ["plan:site"]
    assert dp.package_users(tmp_path, "banc-280", "1.0.0") == ["site"]
    assert dp.package_users(tmp_path, "banc-280", "2.0.0") == []


def test_render_of_the_registry_of_a_source():
    assert dp.render_artefact_registry("amorcage-{{equipe}}", {"equipe": "ops"}, "gitops.x") == "amorcage-ops"
    with pytest.raises(dp.ProfileError) as e:
        dp.render_artefact_registry("amorcage-{{zone}}", {}, "phases.gitops.entries[0].source.registry")
    assert e.value.errors == [("phases.gitops.entries[0].source.registry", "missing variable zone")]


# --- modèles de cluster : options d'accès et sans Internet -------------------------

TEMPLATE = {"k8s_version": "v1.34.11"}
OIDC = {"issuer_url": "https://idp.client/realms/k8s", "audience": "kubernetes", "admin_group": "ops"}


def test_access_options_are_admitted_in_a_cluster_template():
    t = dp.check_capi_template({"spec": dict(TEMPLATE, oidc=OIDC, rancher_external=True, offline=True,
                                             offline_artifacts="image")})
    assert t["spec"]["oidc"] == OIDC and t["spec"]["rancher_external"] is True and t["spec"]["offline"] is True
    v = dp.check_capi_template({"variables": ["idp"], "spec": {"oidc": {"issuer_url": "{{idp}}", "audience": "k"}}})
    assert v["spec"]["oidc"]["issuer_url"] == "{{idp}}"
    t["name"], v["name"] = "m", "m"
    spec = dp.render_capi_template(dict(v, variables=["idp"]), {"idp": "https://idp/x", "cluster": "h"}, "web")
    assert spec["oidc"] == {"issuer_url": "https://idp/x", "audience": "k"} and spec["offline"] is False


@pytest.mark.parametrize("spec,where,why", [
    ({"oidc": "https://idp"}, "spec.oidc", "an object of strings"),
    ({"oidc": {"issuer_url": "https://idp", "audience": "k", "groups": "x"}}, "spec.oidc.groups", "unknown key"),
    ({"oidc": {"issuer_url": "http://idp", "audience": "k"}}, "spec.oidc", "oidc-issuer-invalid"),
    ({"oidc": {"issuer_url": "https://idp", "audience": "k", "username_prefix": "system:"}}, "spec.oidc",
     "oidc-prefix-system"),
    (dict(TEMPLATE, k8s_version="v1.29.4", oidc=OIDC), "spec.oidc", "oidc-version-too-old"),
    ({"rancher_import": True, "rancher_external": True}, "spec.rancher_external", "rancher-import-conflict"),
    ({"offline": True, "offline_artifacts": "usb"}, "spec.offline_artifacts", "fetch or image"),
])
def test_cluster_template_refusals(spec, where, why):
    with pytest.raises(dp.ProfileError) as e:
        dp.check_capi_template({"spec": spec})
    assert any(w == where and why in r for w, r in e.value.errors), e.value.errors
