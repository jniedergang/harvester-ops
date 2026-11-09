"""2.10.0 (tâche 10) : `harvester-capi eso-check`, `eso-deploy`,
`eso-remove`, `eso-status`. Ordre de pose (sonde, chart, authentification
dans OpenBao, objets du magasin, `Ready`), retrait sans perte des Secrets
(point 300), état sans valeur."""

import contextlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_services as sv  # noqa: E402
import capi_stack as cs  # noqa: E402
import child_eso as ce  # noqa: E402

NS, CL, H = "c210", "c210", "harvlab2"
KEY = ce.cluster_key(H, NS, CL)
CID = "abc123def456"
LISTEN = "172.16.1.11:8212"
WITNESS = "valeur-temoin-2100"
JWKS = {"keys": [{"kty": "RSA", "kid": "k8s-1", "n": "AQAB", "e": "AQAB"}]}
STORE = {"server": f"https://{LISTEN}", "ca_pem": "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n",
         "kv_mount": "harvester-ops", "prefix": f"consoles/{CID}", "jwt_mount": f"jwt-hops-{CID}-eso-{KEY}",
         "audience": f"hops-openbao-{CID}"}


@pytest.fixture(scope="module")
def cli():
    s = importlib.util.spec_from_file_location("harvester_capi_eso", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod


def capi_cluster():
    return {"metadata": {"namespace": NS, "name": CL, "labels": {}},
            "spec": {"topology": {"variables": [{"name": "vmNetworks", "value": ["default/lab-net"]},
                                                {"name": "gateway", "value": "172.16.2.1"},
                                                {"name": "subnetMask", "value": "255.255.255.0"}]}}}


def eso_hcp(locked=True):
    values = ce.chart_values() if locked else {"processClusterStore": True}
    return {"metadata": {"name": "eso", "namespace": NS}, "spec": {"valuesTemplate": yaml.safe_dump(values)}}


RELEASE = {"metadata": {"namespace": NS, "ownerReferences": [{"kind": "HelmChartProxy", "name": "eso"}]},
           "spec": {"clusterRef": {"name": CL}}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}


class Mgmt:
    def __init__(self, objs, releases=()):
        self.objs = dict(objs)
        self.releases = list(releases)
        self.kubeconfig = "/nonexistent"

    def get(self, kind, ns, name):
        return self.objs.get((kind, ns, name))

    def list(self, kind, ns=None, selector=None):
        if kind == sv.K_HRP:
            return self.releases
        return [o for (k, s, _), o in self.objs.items() if k == kind and (ns is None or s == ns)]


def world_ok(locked=True, chart=True):
    objs = {(cs.K_CLUSTER, NS, CL): capi_cluster()}
    if chart:
        objs[(sv.K_HCP, NS, "eso")] = eso_hcp(locked)
    return Mgmt(objs, [RELEASE] if chart else [])


class Child:
    def __init__(self, namespaces=("team-a", "team-b"), stores_ready=True, ess=(), secrets=(), stuck=False):
        self.namespaces = {n: {"metadata": {"name": n, "labels": {}}} for n in namespaces}
        self.stores_ready, self.ess, self.secrets, self.stuck = stores_ready, list(ess), list(secrets), stuck
        self.patches, self.deleted, self.events = [], [], None
        self.accounts = {}          # namespace -> uid du compte hops-eso (posé par art-eso)

    def get(self, kind, ns, name):
        if kind == "serviceaccounts" and name == ce.ACCOUNT and ns in self.accounts:
            return {"metadata": {"namespace": ns, "name": name, "uid": self.accounts[ns]}}
        return self.namespaces.get(name) if kind == "Namespace" else None

    def run(self, *a, **kw):
        if a[-1].endswith("openid-configuration"):
            return json.dumps({"issuer": "https://kubernetes.default.svc.cluster.local"})
        return json.dumps(JWKS)

    def list(self, kind, ns=None, selector=None):
        if kind == "Namespace":
            return [n for n in self.namespaces.values()
                    if not selector or (n["metadata"]["labels"].get(ce.NS_LABEL) == "on")]
        if kind == "secretstores.external-secrets.io":
            return [{"metadata": {"namespace": n, "name": ce.STORE},
                     "status": {"conditions": [{"type": "Ready", "status": "True" if self.stores_ready else "False",
                                                "reason": "Valid" if self.stores_ready else "InvalidProviderConfig"}]}}
                    for n in self.namespaces]
        if kind == "externalsecrets.external-secrets.io":
            return list(self.ess)
        if kind == "secrets":
            return [x for x in self.secrets if x["metadata"]["namespace"] == ns]
        return []

    def patch(self, kind, ns, name, patch):
        self.patches.append((kind, ns, name, patch))
        if kind == "Namespace":
            self.namespaces[name]["metadata"]["labels"].update(patch["metadata"]["labels"])
            self.namespaces[name]["metadata"]["labels"] = {k: v for k, v in
                                                            self.namespaces[name]["metadata"]["labels"].items() if v}
        if kind == "externalsecrets.external-secrets.io" and self.stuck == "until-finalizers":
            self.ess = [e for e in self.ess if e["metadata"]["name"] != name]

    def delete(self, kind, ns, name, **kw):
        self.deleted.append((kind, ns, name))
        if kind == "externalsecrets.external-secrets.io" and not self.stuck:
            self.ess = [e for e in self.ess if e["metadata"]["name"] != name]


@pytest.fixture
def world(tmp_path, monkeypatch, cli):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "_state", lambda: state)
    monkeypatch.setattr(cli, "ESO_REMOVE_WAIT", 0)
    monkeypatch.setattr(cli, "ESO_STUCK_WAIT", 0)
    monkeypatch.setattr(cli, "ESO_ACCOUNT_WAIT", 0)
    box = {"child": Child(), "events": [], "delivered": [], "probe": {"ok": True}, "secrets_out": None}

    def settings(**kw):
        doc = {"mode": "b1", "addr": "https://127.0.0.1:8210", "console_id": CID, "prefix": f"consoles/{CID}",
               "eso": {"listener": LISTEN, "address": LISTEN, "clusters": {}}}
        doc.update(kw)
        (state / "secrets-settings.json").write_text(json.dumps(doc))
    settings()

    def secrets_tool(argv, private):
        box["events"].append(("secrets", list(argv), dict(private or {})))
        return box["secrets_out"] or (0, {"ok": True, "applied": True, "commands": None, "store": dict(STORE)})

    def probe(state_, harvester, kube, network, endpoints, image):
        box["events"].append(("probe", network, list(endpoints)))
        if box["probe"] is None:
            return {"status": "unchecked", "reason": "image-missing", "results": {}}
        return {"status": "probed", "results": {endpoints[0]: dict(box["probe"], failed=None if box["probe"]["ok"]
                                                                    else "tcp")}}

    def run(mgmt, argv, spec=None):
        if spec is not None:
            p = tmp_path / "spec.json"
            p.write_text(json.dumps(spec))
            argv = argv + ["--spec", str(p)]
        monkeypatch.setattr(cli, "kube_from", lambda a: (mgmt, "/x", None))

        @contextlib.contextmanager
        def session(k, ns, c):
            yield "/nonexistent"
        monkeypatch.setattr(cli.ca, "session", session)
        monkeypatch.setattr(cli, "Kube", lambda path, **kw: box["child"])
        monkeypatch.setattr(cli, "SECRETS_RUNNER", secrets_tool)
        monkeypatch.setattr(cli, "VM_PROBE", probe)

        def deliver(*a, **k):
            box["events"].append(("deliver", a[3]))
            box["delivered"].append((a, k))
            # le ClusterResourceSet pose le compte hops-eso de chaque namespace
            ch = box["child"]
            for i, n in enumerate(sorted(ch.namespaces) if not box.get("no_accounts") else []):
                ch.accounts.setdefault(n, f"{i + 1:08x}-0000-4000-8000-000000000e50")
            return 0
        monkeypatch.setattr(cli, "deliver", deliver)
        return cli.main(argv + ["--harvester-name", H])
    return {"run": run, "box": box, "settings": settings}


def req(**over):
    r = {"cluster": f"{NS}/{CL}", "namespaces": ["team-a", "team-b"], "by": "alice", "role": "admin"}
    r.update(over)
    return r


# ---------------------------------------------------------------------------
# Constats
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mgmt,over,settings,code", [
    (world_ok, {}, {"mode": "local"}, "eso-needs-openbao"),
    (world_ok, {"role": "operator"}, {}, "eso-admin-only"),
    (lambda: world_ok(chart=False), {}, {}, "eso-chart-missing"),
    (lambda: world_ok(locked=False), {}, {}, "eso-chart-not-locked"),
    (lambda: Mgmt({}), {}, {}, "eso-cluster-missing"),
    (world_ok, {"namespaces": []}, {}, "eso-no-namespace"),
    (world_ok, {}, {"eso": {"listener": None, "address": None, "clusters": {}}}, "eso-listener-needed"),
    (world_ok, {}, {"mode": "a", "addr": "http://127.0.0.1:8200"}, "eso-openbao-not-https"),
])
def test_check_findings(cli, world, capsys, mgmt, over, settings, code):
    world["settings"](**settings)
    assert world["run"](mgmt(), ["eso-check", "--kubeconfig", "/x"], req(**over)) == 2
    assert code in [f["code"] for f in json.loads(capsys.readouterr().out)["findings"]]


def test_check_refuses_a_system_namespace(cli, world, capsys):
    assert world["run"](world_ok(), ["eso-check", "--kubeconfig", "/x"], req(namespaces=["kube-system"])) == 2
    assert "system namespace" in capsys.readouterr().err


def test_check_probes_openbao_from_the_vm_network(cli, world, capsys):
    """Mode A : la sonde juge l'adresse d'OpenBao depuis le réseau des VMs."""
    world["settings"](mode="a", addr="https://bao.example:8200")
    world["box"]["probe"] = {"ok": False}
    assert world["run"](world_ok(), ["eso-check", "--kubeconfig", "/x"], req()) == 2
    out = json.loads(capsys.readouterr().out)
    assert [f["code"] for f in out["findings"]] == ["eso-openbao-unreachable"]
    assert world["box"]["events"] == [("probe", "default/lab-net", ["bao.example:8200"])]
    world["box"]["probe"] = None
    assert world["run"](world_ok(), ["eso-check", "--kubeconfig", "/x"], req()) == 0
    assert [f["level"] for f in json.loads(capsys.readouterr().out)["findings"]] == ["warn"]


def test_check_b1_listener_off_is_a_warning(cli, world, capsys):
    world["settings"](eso={"listener": None, "address": LISTEN, "clusters": {}})
    assert world["run"](world_ok(), ["eso-check", "--kubeconfig", "/x"], req()) == 0
    out = json.loads(capsys.readouterr().out)
    assert [(f["code"], f["level"]) for f in out["findings"]] == [("eso-openbao-unchecked", "warn")]
    assert out["endpoint"] == f"https://{LISTEN}" and out["key"] == KEY


# ---------------------------------------------------------------------------
# Pose
# ---------------------------------------------------------------------------
def test_deploy_b1_in_order(cli, world, capsys):
    box = world["box"]
    assert world["run"](world_ok(), ["eso-deploy", "--kubeconfig", "/x"], req()) == 0
    kinds = [e[0] for e in box["events"]]
    # authentification, sonde, magasins, puis rôle lié aux comptes hops-eso posés (revue 8-10 I-3)
    assert kinds == ["secrets", "probe", "deliver", "secrets"]
    _s, argv, private = box["events"][0]
    assert argv == ["eso", "add", "--cluster", f"{H}/{NS}/{CL}", "--namespace", "team-a", "--namespace", "team-b",
                    "--node-cidr", "172.16.2.0/24"]
    assert json.loads(private["eso_jwks"]) == JWKS and private["eso_issuer"].startswith("https://kubernetes")
    assert box["events"][1] == ("probe", "default/lab-net", [LISTEN])
    (args, _kw), = box["delivered"]
    assert args[1:6] == (NS, CL, "eso", "harvester-ops", "eso")
    assert args[6] == ce.store_objects(["team-a", "team-b"], STORE["server"], STORE["kv_mount"], STORE["jwt_mount"],
                                       STORE["audience"], STORE["ca_pem"])
    assert args[8] == "alice"
    child = box["child"]
    assert {n: d["metadata"]["labels"].get(ce.NS_LABEL) for n, d in child.namespaces.items()} == \
        {"team-a": "on", "team-b": "on"}
    _s, argv2, private2 = box["events"][3]
    assert argv2 == argv + ["--sa-uid", f"team-a={child.accounts['team-a']}",
                            "--sa-uid", f"team-b={child.accounts['team-b']}"]
    assert private2 == private
    assert "2 store(s) ready" in capsys.readouterr().err


def test_deploy_unlabels_a_namespace_left_out(cli, world, capsys):
    child = world["box"]["child"]
    child.namespaces["team-b"]["metadata"]["labels"][ce.NS_LABEL] = "on"
    assert world["run"](world_ok(), ["eso-deploy", "--kubeconfig", "/x"], req(namespaces=["team-a"])) == 0
    assert child.namespaces["team-b"]["metadata"]["labels"] == {}
    assert child.namespaces["team-a"]["metadata"]["labels"] == {ce.NS_LABEL: "on"}


def test_deploy_refuses_a_missing_namespace_before_openbao(cli, world, capsys):
    world["box"]["child"] = Child(namespaces=("team-a",))
    assert world["run"](world_ok(), ["eso-deploy", "--kubeconfig", "/x"], req()) == 2
    assert "eso-namespace-missing" in capsys.readouterr().err
    assert world["box"]["events"] == []


def test_deploy_b1_unreachable_listener_stops_before_the_stores(cli, world, capsys):
    world["box"]["probe"] = {"ok": False}
    assert world["run"](world_ok(), ["eso-deploy", "--kubeconfig", "/x"], req()) == 2
    assert [e[0] for e in world["box"]["events"]] == ["secrets", "probe"]
    assert "eso-openbao-unreachable" in capsys.readouterr().err


def test_deploy_mode_a_hands_the_commands_and_does_not_wait(cli, world, capsys):
    world["settings"](mode="a", addr="https://bao.example:8200")
    world["box"]["secrets_out"] = (0, {"ok": True, "applied": False, "commands": ["bao auth enable ..."],
                                       "store": dict(STORE, server="https://bao.example:8200")})
    world["box"]["child"] = Child(stores_ready=False)
    assert world["run"](world_ok(), ["eso-deploy", "--kubeconfig", "/x"], req()) == 0
    o = capsys.readouterr()
    assert json.loads(o.out.strip().splitlines()[-1])["commands"] == ["bao auth enable ..."]
    assert "OpenBao administrator" in o.err
    assert [e[0] for e in world["box"]["events"]] == ["probe", "secrets", "deliver", "secrets"]


def test_deploy_stores_not_ready_fail_with_the_reason(cli, world, capsys):
    world["box"]["child"] = Child(stores_ready=False)
    assert world["run"](world_ok(), ["eso-deploy", "--kubeconfig", "/x", "--timeout", "1"], req()) == 1
    assert "InvalidProviderConfig" in capsys.readouterr().err


def test_deploy_openbao_refusal_stops(cli, world, capsys):
    world["box"]["secrets_out"] = (2, {"ok": False, "code": "eso-listener-needed", "error": "no address"})
    assert world["run"](world_ok(), ["eso-deploy", "--kubeconfig", "/x"], req()) == 1
    assert "eso-listener-needed" in capsys.readouterr().err and world["box"]["delivered"] == []


def test_deploy_with_chart_installs_the_service_first(cli, world, capsys, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_service_deploy", lambda a: seen.append(json.loads(Path(a.spec).read_text())) or 0)
    monkeypatch.setattr(cli, "service_source", lambda k, s, h, read_creds=False: (s, [], None))
    chart = {"registry": "amorcage", "package": "eso-pack", "version": "2.14.0"}
    assert world["run"](world_ok(chart=False), ["eso-deploy", "--kubeconfig", "/x"], req(chart=chart)) == 0
    assert seen == [{"cluster": f"{NS}/{CL}", "name": "eso", "source": dict(chart, item="eso"), "by": "alice"}]


# ---------------------------------------------------------------------------
# Retrait (point 300)
# ---------------------------------------------------------------------------
def _es(ns, name, uid):
    return {"metadata": {"namespace": ns, "name": name, "uid": uid, "finalizers": [ce.FINALIZER]},
            "spec": {"secretStoreRef": {"name": ce.STORE, "kind": "SecretStore"},
                     "data": [{"remoteRef": {"key": WITNESS}}]}}


def _sec(ns, name, owner_uid):
    return {"metadata": {"namespace": ns, "name": name, "ownerReferences": [
        {"apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret", "uid": owner_uid, "name": name}]}}


@pytest.fixture
def removed(cli, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_manifest_remove", lambda a: calls.append(("manifest", a.name, a.expect_run)) or 0)
    monkeypatch.setattr(cli, "cmd_service_remove", lambda a: calls.append(("service", a.name)) or 0)
    return calls


def test_remove_keeps_the_synced_secrets_then_removes_in_order(cli, world, capsys, removed):
    _enabled(world)
    box = world["box"]
    box["child"] = Child(ess=[_es("team-a", "db", "u1")], secrets=[_sec("team-a", "db", "u1"),
                                                                  _sec("team-a", "other", "u-x")])
    assert world["run"](world_ok(), ["eso-remove", "--kubeconfig", "/x", "--name", f"{NS}/{CL}", "--by", "alice"]) == 0
    child = box["child"]
    assert child.patches[0] == ("secrets", "team-a", "db", {"metadata": {"ownerReferences": None}})
    assert not [p for p in child.patches if p[2] == "other"]
    assert child.deleted == [("externalsecrets.external-secrets.io", "team-a", "db")]
    assert removed == [("manifest", f"{NS}/{CL}/eso", None), ("service", f"{NS}/eso")]
    assert box["events"][-1] == ("secrets", ["eso", "remove", "--cluster", f"{H}/{NS}/{CL}"], {})
    assert WITNESS not in capsys.readouterr().err


def test_remove_keep_chart(cli, world, capsys, removed):
    assert world["run"](world_ok(), ["eso-remove", "--kubeconfig", "/x", "--name", f"{NS}/{CL}", "--keep-chart"]) == 0
    assert removed == [("manifest", f"{NS}/{CL}/eso", None)]


def test_remove_stuck_finalizers_are_taken_off(cli, world, capsys, removed):
    _enabled(world)
    world["box"]["child"] = Child(ess=[_es("team-a", "db", "u1")], stuck="until-finalizers")
    assert world["run"](world_ok(), ["eso-remove", "--kubeconfig", "/x", "--name", f"{NS}/{CL}"]) == 0
    assert ("externalsecrets.external-secrets.io", "team-a", "db",
            {"metadata": {"finalizers": None}}) in world["box"]["child"].patches


def test_remove_stuck_for_good(cli, world, capsys, removed):
    _enabled(world)
    world["box"]["child"] = Child(ess=[_es("team-a", "db", "u1")], stuck=True)
    assert world["run"](world_ok(), ["eso-remove", "--kubeconfig", "/x", "--name", f"{NS}/{CL}"]) == 1
    assert "eso-remove-stuck" in capsys.readouterr().err and removed == []


def test_remove_is_for_administrators(cli, world, capsys, removed):
    assert world["run"](world_ok(), ["eso-remove", "--kubeconfig", "/x", "--name", f"{NS}/{CL}",
                                     "--role", "operator"]) == 2
    assert removed == [] and world["box"]["events"] == []


# ---------------------------------------------------------------------------
# État
# ---------------------------------------------------------------------------
def test_status_without_any_value(cli, world, capsys):
    world["settings"](eso={"listener": LISTEN, "clusters": {KEY: {"mount": STORE["jwt_mount"], "keys_sha256": "ab",
                                                                    "applied": True, "namespaces": ["team-a"]}}})
    es = _es("team-a", "db", "u1")
    es["status"] = {"refreshTime": "2026-11-08T10:00:00Z", "conditions": [{"type": "Ready", "status": "True",
                                                                           "reason": "SecretSynced"}]}
    es["data"] = {"password": WITNESS}
    world["box"]["child"] = Child(ess=[es])
    world["box"]["child"].namespaces["team-a"]["metadata"]["labels"][ce.NS_LABEL] = "on"
    assert world["run"](world_ok(), ["eso-status", "--kubeconfig", "/x", "--name", f"{NS}/{CL}", "--json"]) == 0
    out = capsys.readouterr().out
    assert WITNESS not in out
    doc = json.loads(out)
    assert doc["key"] == KEY and doc["chart"] == {"present": True, "ready": True, "locked": True}
    assert doc["namespaces"] == ["team-a"] and doc["labelled"] == ["team-a"]
    assert doc["externalsecrets"][0]["reason"] == "SecretSynced"
    assert doc["example"]["spec"]["refreshInterval"] == "1h"
    assert doc["endpoint"] == f"https://{LISTEN}"


def test_deleting_the_child_cluster_removes_its_eso_authentication(cli, world, capsys, monkeypatch):
    """Tâche 9 : « montage d'un cluster supprimé retiré »."""
    world["settings"](eso={"listener": LISTEN, "clusters": {KEY: {"mount": STORE["jwt_mount"]}}})
    monkeypatch.setattr(cli, "remove_cluster", lambda kube, ns, name, timeout: 0)
    assert world["run"](world_ok(), ["delete", "--kubeconfig", "/x", "--name", f"{NS}/{CL}"]) == 0
    assert world["box"]["events"] == [("secrets", ["eso", "remove", "--cluster", f"{H}/{NS}/{CL}"], {})]
    world["box"]["events"].clear()
    world["settings"](eso={"listener": LISTEN, "clusters": {}})
    assert world["run"](world_ok(), ["delete", "--kubeconfig", "/x", "--name", f"{NS}/{CL}"]) == 0
    assert world["box"]["events"] == []


# ---------------------------------------------------------------------------
# Revue 8-10 I-1 : le retrait ne touche que NOS ExternalSecret (magasin
# hops-openbao, namespace activé, jamais livrés par Flux) et leurs Secrets
# ---------------------------------------------------------------------------
def _es_on(ns, name, uid, store=ce.STORE, kind="SecretStore", labels=None):
    es = _es(ns, name, uid)
    es["spec"]["secretStoreRef"] = {"name": store, **({"kind": kind} if kind else {})}
    if labels:
        es["metadata"]["labels"] = labels
    return es


def _enabled(world, namespaces=("team-a",)):
    world["settings"](eso={"listener": LISTEN, "clusters": {KEY: {"mount": STORE["jwt_mount"],
                                                                    "namespaces": list(namespaces)}}})


@pytest.mark.parametrize("es,ours", [
    (_es_on("team-a", "db", "u1"), True),
    (_es_on("team-a", "db", "u1", kind=None), True),                         # sorte absente = SecretStore
    (_es_on("team-a", "aws", "u2", store="aws-store"), False),                # autre magasin
    (_es_on("team-a", "cl", "u3", kind="ClusterSecretStore"), False),
    (_es_on("team-c", "db", "u4"), False),                                   # namespace non activé
    (_es_on("team-a", "flux", "u5", labels={"kustomize.toolkit.fluxcd.io/name": "apps"}), False),
    (_es_on("team-a", "helm", "u6", labels={"helm.toolkit.fluxcd.io/name": "r"}), False),
])
def test_ours_table(es, ours):
    mine, foreign = ce.ours([es], ["team-a"])
    assert (mine == [es], foreign == [es]) == (ours, not ours)


def test_remove_leaves_foreign_externalsecrets_and_their_secrets(cli, world, capsys, removed):
    _enabled(world)
    box = world["box"]
    mine = _es_on("team-a", "db", "u1")
    other = _es_on("team-a", "aws", "u2", store="aws-store")
    off = _es_on("team-c", "db", "u3")
    box["child"] = Child(ess=[mine, other, off], secrets=[_sec("team-a", "db", "u1"), _sec("team-a", "aws", "u2"),
                                                         _sec("team-c", "db", "u3")])
    assert world["run"](world_ok(), ["eso-remove", "--kubeconfig", "/x", "--name", f"{NS}/{CL}", "--keep-chart"]) == 0
    child = box["child"]
    assert [p[:3] for p in child.patches if p[0] == "secrets"] == [("secrets", "team-a", "db")]
    assert child.deleted == [("externalsecrets.external-secrets.io", "team-a", "db")]
    assert "2 ExternalSecret(s) of the teams left" in capsys.readouterr().err


def test_remove_with_the_chart_blocks_on_foreign_externalsecrets(cli, world, capsys, removed):
    """Le chart emporte les CRD : des objets ESO des équipes restants seraient
    perdus ou bloqueraient la suppression. Refus avant tout geste."""
    _enabled(world)
    box = world["box"]
    box["child"] = Child(ess=[_es_on("team-a", "db", "u1"), _es_on("team-c", "x", "u9")],
                         secrets=[_sec("team-a", "db", "u1")])
    assert world["run"](world_ok(), ["eso-remove", "--kubeconfig", "/x", "--name", f"{NS}/{CL}"]) == 2
    err = capsys.readouterr().err
    assert "eso-foreign-objects" in err and "team-c/x" in err
    assert box["child"].patches == [] and box["child"].deleted == [] and removed == [] and box["events"] == []


# ---------------------------------------------------------------------------
# Revue 8-10 I-2 : le Défaire relit la marque de l'exécution AVANT tout geste
# ---------------------------------------------------------------------------
def _crs(run):
    import capi_manifests as mf
    return {"metadata": {"namespace": NS, "name": mf.name_of(ce.ITEM),
                         "labels": {mf.L_CLUSTER: CL, mf.L_ITEM: ce.ITEM},
                         "annotations": {"harvester-ops.io/plan-run": run}}}


@pytest.mark.parametrize("on_crs,expect,ok", [("run-new", "run-old", False), ("run-old", "run-old", True)])
def test_undo_checks_the_run_before_touching_anything(cli, world, capsys, removed, on_crs, expect, ok):
    import capi_manifests as mf
    _enabled(world)
    box = world["box"]
    box["child"] = Child(ess=[_es("team-a", "db", "u1")], secrets=[_sec("team-a", "db", "u1")])
    mgmt = world_ok()
    mgmt.objs[(mf.K_CRS, NS, mf.name_of(ce.ITEM))] = _crs(on_crs)
    rc = world["run"](mgmt, ["eso-remove", "--kubeconfig", "/x", "--name", f"{NS}/{CL}", "--keep-chart",
                             "--expect-run", expect])
    if ok:
        assert rc == 0 and box["child"].deleted
        return
    assert rc == 2 and "undo-not-ours" in capsys.readouterr().err
    assert box["child"].patches == [] and box["child"].deleted == [] and removed == [] and box["events"] == []



# ---------------------------------------------------------------------------
# Revue 8-10 I-3 : rôle lié aux UID des comptes hops-eso ; namespace disparu
# ou recréé signalé
# ---------------------------------------------------------------------------
def test_redeploy_passes_the_known_accounts_at_once(cli, world, capsys):
    """Réactivation : les comptes déjà posés sont liés dès le premier appel
    (jamais de fenêtre liée au seul nom), et rien n'est rejoué s'ils n'ont
    pas changé."""
    box = world["box"]
    box["child"].accounts = {"team-a": "00000001-0000-4000-8000-000000000e50",
                             "team-b": "00000002-0000-4000-8000-000000000e50"}
    assert world["run"](world_ok(), ["eso-deploy", "--kubeconfig", "/x"], req()) == 0
    secrets_calls = [e for e in box["events"] if e[0] == "secrets"]
    assert len(secrets_calls) == 1
    assert secrets_calls[0][1][-4:] == ["--sa-uid", "team-a=00000001-0000-4000-8000-000000000e50",
                                        "--sa-uid", "team-b=00000002-0000-4000-8000-000000000e50"]


def test_deploy_fails_when_an_account_never_comes(cli, world, capsys):
    box = world["box"]
    box["no_accounts"] = True                                       # ClusterResourceSet jamais appliqué
    assert world["run"](world_ok(), ["eso-deploy", "--kubeconfig", "/x"], req()) == 1
    assert "eso-account-missing" in capsys.readouterr().err
    assert [e[0] for e in box["events"]] == ["secrets", "probe", "deliver"]


@pytest.mark.parametrize("namespaces,accounts,bound,codes", [
    (["team-a"], {"team-a": "u1"}, {"team-a": "u1"}, []),
    (["team-a"], {"team-a": "u2"}, {"team-a": "u1"}, ["eso-account-changed"]),       # compte ou namespace recréé
    (["team-a", "gone"], {"team-a": "u1"}, {"team-a": "u1", "gone": "u9"}, ["eso-namespace-gone"]),
    (["team-a"], {}, {"team-a": "u1"}, ["eso-account-changed"]),                       # compte supprimé
    (["team-a"], {"team-a": "u1"}, {}, []),                                            # mode d'avant : rien à comparer
])
def test_account_findings(cli, namespaces, accounts, bound, codes):
    child = Child(namespaces=[n for n in namespaces if n != "gone"])
    child.accounts = dict(accounts)
    assert [f["code"] for f in cli._eso_account_findings(child, namespaces, bound)] == codes


# ---------------------------------------------------------------------------
# Revue finale I-1 : le retrait du chart `eso` lui-même (Défaire, onglet avec
# ?keep_chart=1 puis Artefacts, `service-remove`) est refusé tant qu'un
# ExternalSecret existe dans un cluster enfant visé : le chart emporterait
# les CRD (finalizers orphelins, Secrets des équipes ramassés ensuite)
# ---------------------------------------------------------------------------
def _labelled_world():
    mgmt = world_ok()
    mgmt.patch = lambda *a, **kw: None
    mgmt.objs[(cs.K_CLUSTER, NS, CL)]["metadata"]["labels"][sv.label_of(ce.SERVICE)] = "true"
    return mgmt


@pytest.mark.parametrize("ess,rc,deleted", [
    ([_es_on("team-c", "x", "u9"), _es_on("team-a", "aws", "u2", store="aws-store")], 2, False),
    ([], 0, True),
])
def test_service_remove_of_eso_refuses_while_externalsecrets_remain(cli, world, capsys, ess, rc, deleted):
    world["box"]["child"] = Child(ess=ess)
    mgmt = _labelled_world()
    got = []
    mgmt.delete = lambda kind, ns, name, **kw: got.append((kind, ns, name))
    assert world["run"](mgmt, ["service-remove", "--kubeconfig", "/x", "--name", f"{NS}/eso",
                               "--timeout", "0"]) == rc
    assert bool(got) is deleted
    if not deleted:
        err = capsys.readouterr().err
        assert "eso-foreign-objects" in err and "team-a/aws" in err and "team-c/x" in err


def test_service_remove_of_eso_refuses_when_the_child_cannot_be_read(cli, world, capsys):
    class Broken(Child):
        def list(self, kind, ns=None, selector=None):
            if kind == "externalsecrets.external-secrets.io":
                raise cli.KubeError("connection refused")
            return super().list(kind, ns, selector)
    world["box"]["child"] = Broken()
    mgmt = _labelled_world()
    got = []
    mgmt.delete = lambda kind, ns, name, **kw: got.append((kind, ns, name))
    assert world["run"](mgmt, ["service-remove", "--kubeconfig", "/x", "--name", f"{NS}/eso",
                               "--timeout", "0"]) == 2
    assert got == [] and "eso-status-unreadable" in capsys.readouterr().err


def test_service_remove_of_another_chart_does_not_read_the_child(cli, world, capsys):
    class NoRead(Child):
        def list(self, kind, ns=None, selector=None):
            raise AssertionError("child read")
    world["box"]["child"] = NoRead()
    mgmt = world_ok()
    mgmt.objs[(sv.K_HCP, NS, "podinfo")] = {"metadata": {"name": "podinfo", "namespace": NS}, "spec": {}}
    mgmt.delete = lambda kind, ns, name, **kw: None
    assert world["run"](mgmt, ["service-remove", "--kubeconfig", "/x", "--name", f"{NS}/podinfo",
                               "--timeout", "0"]) == 0
