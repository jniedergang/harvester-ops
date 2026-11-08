"""2.8.1 (décision 242) : `harvester-nodes` avertit quand le registre est
hébergé par le cluster Harvester même.

Vu sur harvlab2 (R10 de 2.8.0) : le nœud porte l'adresse kube-vip du
registre d'amorçage, une connexion du nœud vers elle expire, containerd
retombe sur le registre d'origine. Constat `harvester-nodes-self-hosted`,
niveau `warn` (jamais bloquant) : registre d'amorçage déclaré sur ce
cluster, ou point d'accès égal à l'adresse d'un Service LoadBalancer du
cluster. Rendu en JSON (`findings`) et dans le journal de l'action.

Mutations jouées (rapport 2.8.1) : « constat bloquant », « port non
retiré de l'adresse », « constat aussi au retrait ».
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import registries as rg  # noqa: E402
from test_registry_nodes_280 import CURRENT, Kube  # noqa: E402

SPEC = importlib.util.spec_from_file_location("harvester_registry_nodes_281", ROOT / "bin" / "harvester-registry.py")
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)
I18N = (ROOT / "web" / "static" / "js" / "i18n.js").read_text()
JS = (ROOT / "web" / "static" / "js" / "registries.js").read_text()
CODE = "harvester-nodes-self-hosted"


def lb(ip, name="registry", ns="harvester-ops-registry", field="ingress"):
    svc = {"metadata": {"name": name, "namespace": ns}, "spec": {"type": "LoadBalancer"}, "status": {}}
    if field == "ingress":
        svc["status"] = {"loadBalancer": {"ingress": [{"ip": ip}]}}
    else:
        svc["spec"]["loadBalancerIP"] = ip
    return svc


class World(Kube):
    def __init__(self, value, services=None, fail=False):
        super().__init__(value)
        self.services = services or []
        self.fail = fail
        self.listed = []

    def list(self, kind, ns=None, selector=None):
        if kind == "services":
            self.listed.append((kind, ns, selector))
            if self.fail:
                raise cli.KubeError("forbidden")
            return list(self.services)
        return super().list(kind, ns, selector)


DECLS = {
    "external": {"kind": "external", "endpoint": "172.16.2.76", "ca_pem": None, "usage": ["images"],
                 "mirrors": ["docker.io"], "clusters": ["harvlab2"]},
    "external-port": {"kind": "external", "endpoint": "172.16.2.76:5000", "ca_pem": None, "usage": ["images"],
                      "mirrors": ["docker.io"], "clusters": ["harvlab2"]},
    "external-name": {"kind": "external", "endpoint": "registry.client.lan", "ca_pem": None, "usage": ["images"],
                      "mirrors": ["docker.io"], "clusters": ["harvlab2"]},
    "bootstrap-here": {"kind": "bootstrap", "endpoint": "172.16.2.76", "ca_pem": None, "usage": ["images"],
                       "mirrors": ["docker.io"], "clusters": ["harvlab2"], "cluster": "harvlab2",
                       "address": "172.16.2.76", "size_gib": 20, "storage_class": "harv-rep1"},
    "bootstrap-other": {"kind": "bootstrap", "endpoint": "172.16.3.76", "ca_pem": None, "usage": ["images"],
                        "mirrors": ["docker.io"], "clusters": ["harvlab2", "harv4"], "cluster": "harv4",
                        "address": "172.16.3.76", "size_gib": 20, "storage_class": "harv-rep1"},
}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    monkeypatch.setattr(cli.rg, "read_creds", lambda k, n, role: {"username": "pull", "password": "PW-281"})

    def make(decl, services=None, fail=False):
        rg.save(state, "amorcage", dict(DECLS[decl]))
        kube = World(json.dumps(CURRENT), services, fail)
        monkeypatch.setattr(cli, "kube_from", lambda args, need_entry=False: (kube, None))
        return kube
    return make


def run(capsys, *extra):
    code = cli.main(["harvester-nodes", "amorcage", "--cluster", "harvlab2", "--kubeconfig", "/kc", *extra])
    o = capsys.readouterr()
    return code, json.loads(o.out), o.err


@pytest.mark.parametrize("decl,services,fail,facts", [
    ("bootstrap-here", [], False, {"registry": "amorcage", "cluster": "harvlab2", "address": "172.16.2.76",
                                   "by": "bootstrap"}),
    ("external", [lb("172.16.2.76")], False, {"registry": "amorcage", "cluster": "harvlab2",
                                             "address": "172.16.2.76", "by": "harvester-ops-registry/registry"}),
    ("external-port", [lb("172.16.2.76", "reg", "tools")], False,
     {"registry": "amorcage", "cluster": "harvlab2", "address": "172.16.2.76", "by": "tools/reg"}),
    ("external", [lb("172.16.2.76", field="spec")], False,
     {"registry": "amorcage", "cluster": "harvlab2", "address": "172.16.2.76", "by": "harvester-ops-registry/registry"}),
    ("external", [lb("172.16.2.77")], False, None),
    ("external", [], False, None),
    ("external-name", [lb("172.16.2.76")], False, None),       # un nom n'est pas résolu : pas de constat
    ("bootstrap-other", [], False, None),
    ("external", [], True, None),                              # Services illisibles : rien, l'écriture continue
    ("bootstrap-here", [], True, {"registry": "amorcage", "cluster": "harvlab2", "address": "172.16.2.76",
                                  "by": "bootstrap"}),
])
def test_self_hosted_registry_is_a_warning_and_never_blocks(setup, capsys, decl, services, fail, facts):
    """Mutations « constat bloquant », « port non retiré de l'adresse »."""
    kube = setup(decl, services, fail)
    code, out, err = run(capsys)
    assert code == 0 and out["blocked"] is False and out["changed"] is True, err
    assert [n for n, _ in kube.patched] == ["containerd-registry"]          # écrit malgré le constat
    found = [f for f in out["findings"] if f["code"] == CODE]
    if facts is None:
        assert found == [] and CODE not in err
    else:
        assert found == [{"code": CODE, "level": "warn", "facts": facts}] and CODE in err
    if decl.startswith("bootstrap-here"):
        assert kube.listed == []                                             # la déclaration suffit


def test_dry_run_shows_it_too(setup, capsys):
    kube = setup("external", [lb("172.16.2.76")])
    code, out, _err = run(capsys, "--dry-run")
    assert code == 0 and kube.patched == [] and CODE in [f["code"] for f in out["findings"]]


def test_remove_does_not_warn(setup, capsys):
    """Mutation « constat aussi au retrait »."""
    setup("bootstrap-here")
    run(capsys)
    code, out, err = run(capsys, "--remove")
    assert code == 0 and CODE not in [f["code"] for f in out["findings"]] and CODE not in err


@pytest.mark.parametrize("decl,services,want", [
    ("bootstrap-here", [], "bootstrap"),
    ("external", [lb("172.16.2.76")], "harvester-ops-registry/registry"),
    ("external", [{"metadata": {"name": "c", "namespace": "n"}, "spec": {"type": "ClusterIP",
                                                                         "clusterIP": "172.16.2.76"}}], None),
    ("external", [{"metadata": {"name": "x", "namespace": "n"}}], None),
])
def test_self_hosted_pure(decl, services, want):
    d = dict(DECLS[decl], name="amorcage")
    got = rg.self_hosted(d, "harvlab2", services)
    assert (got and got["facts"]["by"]) == want if want else got is None


def test_ui_and_five_languages():
    assert f"'{CODE}': (v) => tr('reg.fd.{CODE}'" in JS
    assert I18N.count(f'"reg.fd.{CODE}"') + I18N.count(f"'reg.fd.{CODE}'") == 5


@pytest.mark.parametrize("decl,fail,skipped", [
    ("external", True, True),                                  # Services illisibles : dit dans le journal
    ("external", False, False),
    ("bootstrap-here", True, False),                           # Services non lus : rien à dire
])
def test_unreadable_services_are_named_in_the_action_log(setup, capsys, decl, fail, skipped):
    """Relecture M-5. Mutation « step retiré »."""
    setup(decl, [], fail)
    code, out, err = run(capsys)
    assert code == 0 and out["blocked"] is False
    assert ("self-hosted check skipped: Services unreadable" in err) is skipped
    if skipped:
        assert "STEP_EVENT|harvester-nodes|running|self-hosted check skipped" in err
