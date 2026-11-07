"""2.6.0 (tâche 11) : format de la phase Registre d'un plan, rendu de ses
registres, et usage des registres et des paquets (plans, modèles, clusters
enfants) avant leur suppression (revue m-15)."""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import capi_cluster as cc  # noqa: E402
import deploy_plans as dp  # noqa: E402
import registries as rg  # noqa: E402


def doc(**registry):
    ph = {"registries": [{"name": "amorcage", "mode": "bootstrap", "address": "{{registre_ip}}",
                          "storage_class": "harv-rep1", "mirrors": ["ghcr.io"]},
                         {"name": "client", "mode": "external"}],
          "publish": [{"package": "socle", "version": "2026.11", "registry": "amorcage"}]}
    ph.update(registry)
    return {"variables": ["registre_ip"], "phases": {"registry": ph}}


def test_a_registry_phase_is_normalized():
    p = dp.check_plan(doc())["phases"]["registry"]
    assert p == {"enabled": True, "timeout_minutes": 10, "publish_timeout_minutes": 60,
                 "registries": [{"name": "amorcage", "mode": "bootstrap", "address": "{{registre_ip}}",
                                 "storage_class": "harv-rep1", "size_gib": 20, "mirrors": ["ghcr.io"]},
                                {"name": "client", "mode": "external"}],
                 "publish": [{"package": "socle", "version": "2026.11", "registry": "amorcage"}]}
    assert dp.enabled_phases(dp.check_plan(doc())) == ["registry"]


@pytest.mark.parametrize("change,where,why", [
    ({"timeout_minutes": 61}, "phases.registry.timeout_minutes", "an integer from 1 to 60"),
    ({"publish_timeout_minutes": 4}, "phases.registry.publish_timeout_minutes", "an integer from 5 to 600"),
    ({"mirrors": {}}, "phases.registry.mirrors", "unknown key"),
    ({"registries": [{"name": "a", "mode": "cloud"}]}, "phases.registry.registries[0].mode", "bootstrap or external"),
    ({"registries": [{"name": "Bad", "mode": "external"}]}, "phases.registry.registries[0].name",
     "RFC 1123, 30 characters at most"),
    ({"registries": [{"name": "bootstrap", "mode": "external"}]}, "phases.registry.registries[0].name",
     "RFC 1123, 30 characters at most"),
    ({"registries": [{"name": "a", "mode": "external"}, {"name": "a", "mode": "external"}]},
     "phases.registry.registries[1].name", "a listed twice"),
    ({"registries": [{"name": "a", "mode": "external", "address": "1.2.3.4"}]},
     "phases.registry.registries[0].address", "only for a bootstrap registry"),
    ({"registries": [{"name": "a", "mode": "external", "password": "x"}]},
     "phases.registry.registries[0].password", "unknown key"),                     # aucun identifiant
    ({"registries": [{"name": "a", "mode": "bootstrap", "storage_class": "s"}]},
     "phases.registry.registries[0].address", "required"),
    ({"registries": [{"name": "a", "mode": "bootstrap", "address": "300.1.1.1", "storage_class": "s"}]},
     "phases.registry.registries[0].address", "invalid value"),
    ({"registries": [{"name": "a", "mode": "bootstrap", "address": "{{x}}", "storage_class": "s"}]},
     "phases.registry.registries[0].address", "undeclared variable x"),
    ({"registries": [{"name": "a", "mode": "bootstrap", "address": "1.1.1.1", "storage_class": "s",
                      "size_gib": 4}]}, "phases.registry.registries[0].size_gib", "an integer from 5 to 2048"),
    ({"registries": [{"name": "a", "mode": "bootstrap", "address": "1.1.1.1", "storage_class": "s",
                      "mirrors": ["https://docker.io"]}]}, "phases.registry.registries[0].mirrors[0]",
     "invalid value"),
    ({"publish": [{"package": "Socle", "version": "1.0.0", "registry": "a"}]}, "phases.registry.publish[0].package",
     "a package name"),
    ({"publish": [{"package": "socle", "version": "v1", "registry": "a"}]}, "phases.registry.publish[0].version",
     "x.y.z or YYYY.MM[.DD][-n]"),
    ({"publish": [{"package": "socle", "version": "1.0.0", "registry": "a", "items": []}]},
     "phases.registry.publish[0]", "{package, version, registry}"),
    ({"publish": [{"package": "s", "version": "1.0.0", "registry": "a"}] * 2}, "phases.registry.publish[1]",
     "s 1.0.0 to a listed twice"),
])
def test_each_refusal_names_its_field(change, where, why):
    with pytest.raises(dp.ProfileError) as e:
        dp.check_plan(doc(**change))
    assert (where, why) in e.value.errors, e.value.errors


def test_a_disabled_phase_of_an_older_plan_keeps_its_free_keys_as_warnings():
    out, warnings = dp.check_plan_warnings({"phases": {"registry": {"enabled": False, "mirrors": {"x": 1}}}})
    assert out["phases"]["registry"]["enabled"] is False
    assert warnings == ["phases.registry.mirrors: unknown key, ignored (phase disabled)"]


def test_artefacts_stays_coming_in_2_7():
    with pytest.raises(dp.ProfileError) as e:
        dp.check_plan({"phases": {"artefacts": {"enabled": True}}})
    assert ("phases.artefacts", "coming in 2.7: set enabled: false") in e.value.errors


@pytest.mark.parametrize("values,ok,error", [
    ({"registre_ip": "172.16.2.76"}, True, None),
    ({}, False, "missing variable registre_ip"),
    ({"registre_ip": "pas-une-ip"}, False, "an IPv4 address"),
])
def test_registries_are_rendered_and_checked_once_rendered(values, ok, error):
    plan = dp.check_plan(doc())
    if ok:
        regs = dp.render_plan_registries(plan, values)
        assert regs[0]["address"] == "172.16.2.76" and regs[1] == {"name": "client", "mode": "external"}
    else:
        with pytest.raises(dp.ProfileError) as e:
            dp.render_plan_registries(plan, values)
        assert any(error in why for _, why in e.value.errors)


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
@pytest.fixture
def state(tmp_path):
    dp.save_plan(tmp_path, "site", doc())
    dp.save_plan(tmp_path, "autre", {"variables": [], "phases": {"clusters": {"enabled": False, "items": [
        {"template": "rke2", "name": "a", "registries": ["miroir"]}]}}})
    dp.save_capi_template(tmp_path, "rke2", {"variables": [], "spec": {"registries": [{"registry": "tpl-reg"}]}})
    return tmp_path


@pytest.mark.parametrize("name,users", [
    ("amorcage", ["plan:site"]),             # registre et publication du plan
    ("client", ["plan:site"]),
    ("miroir", ["plan:autre"]),              # entrée de cluster, phase désactivée comprise
    ("tpl-reg", ["template:rke2"]),
    ("libre", []),
])
def test_registry_users(state, name, users):
    assert dp.registry_users(state, name) == users


def test_package_users(state):
    assert dp.package_users(state, "socle", "2026.11") == ["site"]
    assert dp.package_users(state, "socle", "2026.12") == []


@pytest.fixture(scope="module")
def regcli():
    spec = importlib.util.spec_from_file_location("harvester_registry_260", ROOT / "bin" / "harvester-registry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Kube:
    def __init__(self, secrets=(), fail=False):
        self.secrets, self.fail, self.deleted = list(secrets), fail, []

    def list(self, kind, ns=None, selector=None):
        if self.fail:
            from kube import KubeError
            raise KubeError("Forbidden")
        want = dict(p.split("=", 1) for p in (selector or "").split(",") if p)
        return [s for s in self.secrets if all(s["metadata"].get("labels", {}).get(a) == b for a, b in want.items())]

    def get(self, kind, ns, name):
        return {"metadata": {"name": name}}

    def delete(self, kind, ns, name):
        self.deleted.append((kind, name))


def _child(ns, reg):
    return {"metadata": {"namespace": ns, "labels": {rg.LABEL: reg, cc.GENERATED: "true"}}}


@pytest.mark.parametrize("kube,code", [
    (Kube([_child("web", "amorcage")]), 2),          # cluster enfant
    (Kube(fail=True), 2),                            # usage inconnu : refusé
    (Kube([_child("web", "autre")]), 0),
])
def test_cli_delete_refuses_a_registry_in_use(regcli, monkeypatch, tmp_path, capsys, kube, code):
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(rg, "ca_readable", lambda pem: True)
    rg.save(tmp_path, "amorcage", {"kind": "bootstrap", "endpoint": "172.16.2.76", "usage": ["images"],
                                   "clusters": ["harvlab2"], "cluster": "harvlab2", "address": "172.16.2.76",
                                   "size_gib": 8, "storage_class": "s"})
    monkeypatch.setattr(regcli, "kube_from", lambda a, need_entry=False: (kube, None))
    rc = regcli.cmd_delete(argparse.Namespace(name="amorcage", cluster="harvlab2", kubeconfig="/k",
                                              delete_data=False))
    assert rc == code
    assert (kube.deleted == []) is (code == 2)
    if code:
        assert "registry-in-use" in capsys.readouterr().err or kube.fail


def test_cli_forget_refuses_a_registry_named_by_a_plan(regcli, monkeypatch, state, capsys):
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    rg.save(state, "client", {"kind": "external", "endpoint": "h.example:443", "usage": ["images"],
                              "clusters": ["gone"]})
    assert regcli.cmd_forget(argparse.Namespace(name="client")) == regcli.EXIT_REFUSED
    assert "registry-in-use: plan:site" in capsys.readouterr().err
    assert rg.load(state, "client") is not None


class SecretsKube(Kube):
    """Secrets du registre sur le cluster Harvester ; `refuse` simule un
    droit manquant sur la suppression."""

    def __init__(self, names, refuse=False):
        super().__init__()
        self.names, self.refuse = set(names), refuse

    def delete(self, kind, ns, name):
        from kube import KubeError
        assert kind == "secrets" and ns == rg.NAMESPACE
        if self.refuse:
            raise KubeError(f"Error from server (Forbidden): secrets \"{name}\" is forbidden")
        if name not in self.names:
            raise KubeError(f"Error from server (NotFound): secrets \"{name}\" not found")
        self.names.discard(name)
        self.deleted.append((kind, name))


@pytest.mark.parametrize("names,refuse,code,left", [
    ({"client-push", "client-pull", "client-ca"}, False, 0, set()),
    ({"client-push", "client-pull"}, False, 0, set()),              # sans CA : rien d'autre à dire
    ({"client-push", "client-pull", "client-ca"}, True, 2, {"client-push", "client-pull", "client-ca"}),
])
def test_cli_forget_removes_the_accounts_from_the_attached_clusters(regcli, monkeypatch, tmp_path, capsys,
                                                                    names, refuse, code, left):
    """2.6.0, vu sur harvlab2 : oublier un registre du client laissait son
    compte d'écriture dans les Secrets du cluster Harvester, sans plus rien
    dans la console pour le dire ni le retirer."""
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    rg.save(tmp_path, "client", {"kind": "external", "endpoint": "h.example:443", "usage": ["images"],
                                 "clusters": ["harvlab2"]})
    kube = SecretsKube(names, refuse)
    monkeypatch.setattr(regcli, "_cluster_kube", lambda c: kube)
    assert regcli.cmd_forget(argparse.Namespace(name="client")) == code
    assert kube.names == left
    assert (rg.load(tmp_path, "client") is None) is (code == 0), "the declaration stays for a new try"
    err = capsys.readouterr().err
    assert "Forbidden" in err if refuse else "accounts removed from harvlab2" in err
