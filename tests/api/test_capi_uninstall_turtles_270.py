"""2.7.0 (décisions 178 et 215) : retrait de la pile Turtles posée par la console.

Ce que ces tests tiennent :

- seuls les `CAPIProvider` étiquetés `harvester-ops.io/managed: "true"`
  partent, dans l'ordre CAAPH, CAPHV, plan de contrôle RKE2, amorçage RKE2 ;
  le cœur et Turtles ne sont jamais dans la liste, un fournisseur sans
  l'étiquette est gardé et nommé ;
- Turtles ne refuse aucune suppression (essai 6) : la console refuse
  elle-même tant qu'un Cluster (tout namespace), un HelmChartProxy (CAAPH
  retiré) ou un déroulé de plan existe, et rien n'est supprimé ;
- un déploiement qui ne disparaît pas dans le délai arrête tout, les
  suivants intacts ; les CRD ne sont jamais supprimées ;
- la route lance `harvester-capi uninstall` en action suivie (rôle admin,
  nom retapé), le runner d'avant Turtles reste pour une pile d'avant.
"""

import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "web"))
import capi_stack as cs  # noqa: E402
import node_images  # noqa: E402
from kube import KubeError  # noqa: E402

PROVIDERS = {   # clé -> (namespace, type, nom)
    "rke2-bootstrap": ("rke2-bootstrap-system", "bootstrap", "rke2"),
    "rke2-control-plane": ("rke2-control-plane-system", "controlPlane", "rke2"),
    "harvester": ("caphv-system", "infrastructure", "harvester"),
    "helm": ("caaph-system", "addon", "helm"),
}
ORDER = ["helm", "harvester", "rke2-control-plane", "rke2-bootstrap"]


class FakeKube:
    """kubectl en mémoire ; un CAPIProvider supprimé emporte ses
    déploiements, sauf ceux de `stuck` (namespace)."""

    def __init__(self, managed=tuple(PROVIDERS), extra_unlabelled=False, stuck=(), turtles=True):
        self.objs, self.deleted, self.stuck, self.turtles = {}, [], set(stuck), turtles
        self.put(cs.K_CAPIPROVIDER, "cattle-capi-system", "cluster-api",
                 {"metadata": {"labels": {cs.MANAGED: "true"}},       # même étiqueté : jamais
                  "spec": {"name": "cluster-api", "type": "core", "version": "v1.13.3"},
                  "status": {"phase": "Ready"}})
        for key in managed:
            ns, typ, name = PROVIDERS[key]
            self.put(cs.K_CAPIPROVIDER, ns, key, {"metadata": {"labels": {cs.MANAGED: "true"}},
                                                  "spec": {"name": name, "type": typ, "version": "v1"},
                                                  "status": {"phase": "Ready"}})
            self.put("deployments", ns, f"{key}-controller",
                     {"spec": {"template": {"spec": {"containers": [{"image": f"reg.example/{key}:v1"}]}}}})
            self.put("configmaps", ns, "v1", {"metadata": {"labels": {cs.MANAGED: "true"}}})
            self.put("configmaps", ns, "other", {})
        if extra_unlabelled:
            self.put(cs.K_CAPIPROVIDER, "capk-system", "kubevirt",
                     {"spec": {"name": "kubevirt", "type": "infrastructure", "version": "v0.1"}})
        for crd in ("helmchartproxies.addons.cluster.x-k8s.io", "harvesterclusters.infrastructure.cluster.x-k8s.io"):
            self.put(cs.K_CRD, None, crd, {})
        self.put("services", cs.SHIM_NS, cs.SHIM_NAME,
                 {"metadata": {"labels": {cs.SHIM_LABEL: "caphv-ingress-expose", cs.MANAGED: "true"}}})

    def put(self, kind, ns, name, body):
        body = json.loads(json.dumps(body))
        md = body.setdefault("metadata", {})
        md["name"] = name
        if ns:
            md["namespace"] = ns
        self.objs[(kind, ns, name)] = body

    def get(self, kind, ns, name):
        o = self.objs.get((kind, ns, name))
        return json.loads(json.dumps(o)) if o is not None else None

    def list(self, kind, ns=None, selector=None, missing_ok=True):
        if kind == cs.K_CAPIPROVIDER and not self.turtles:
            raise KubeError("the server doesn't have a resource type \"capiproviders\"")
        key, _, val = (selector or "").partition("=")
        return [json.loads(json.dumps(o)) for (k, n, _), o in self.objs.items()
                if k == kind and (ns is None or n == ns)
                and (not key or ((o.get("metadata") or {}).get("labels") or {}).get(key) == val)]

    def delete(self, kind, ns, name, **kw):
        self.deleted.append((kind, ns, name))
        self.objs.pop((kind, ns, name), None)
        if kind == cs.K_CAPIPROVIDER and ns not in self.stuck:
            for k in [k for k in self.objs if k[0] == "deployments" and k[1] == ns]:
                self.objs.pop(k)


def steps():
    seen = []
    return seen, (lambda sid, status, msg="": seen.append((sid, status, msg)))


def provider_deletions(kube):
    return [n for k, _, n in kube.deleted if k == cs.K_CAPIPROVIDER]


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


# ---------------------------------------------------------------------------
# Le plan et ses refus (tableau du plan, tâche 7)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("setup,code,facts", [
    (lambda k: k.put(cs.K_CLUSTER, "equipe-b", "web", {}), "capi-clusters-present", {"clusters": "equipe-b/web"}),
    (lambda k: (k.put(cs.K_CLUSTER, "a", "one", {}), k.put(cs.K_CLUSTER, "b", "two", {})),
     "capi-clusters-present", {"clusters": "a/one, b/two", "count": 2}),
    (lambda k: k.put(cs.K_HCP, "web", "podinfo", {}), "caaph-services-present", {"services": "web/podinfo"}),
])
def test_refusals_delete_nothing(setup, code, facts):
    k = FakeKube()
    setup(k)
    seen, step = steps()
    plan = cs.uninstall(k, step)
    assert plan["blocked"] and [f["code"] for f in plan["refusals"]] == [code]
    for key, val in facts.items():
        assert plan["refusals"][0]["facts"][key] == val
    assert k.deleted == []
    assert seen[-1][1] == "error" and code in seen[-1][2]


def test_a_running_plan_deployment_refuses():
    k = FakeKube()
    plan = cs.uninstall(k, steps()[1], running={"run": "abc123abc123", "plan": "site-a"})
    assert [f["code"] for f in plan["refusals"]] == ["capi-plan-running"]
    assert plan["refusals"][0]["facts"] == {"run": "abc123abc123", "plan": "site-a"} and k.deleted == []


def test_the_rancher_local_cluster_does_not_refuse():
    k = FakeKube()
    k.put(cs.K_CLUSTER, "fleet-local", "local", {})
    assert not cs.uninstall_plan(k)["blocked"]


def test_helmchartproxies_do_not_refuse_when_caaph_is_not_removed():
    k = FakeKube(managed=("rke2-bootstrap", "rke2-control-plane", "harvester"))
    k.put(cs.K_HCP, "web", "podinfo", {})
    assert not cs.uninstall_plan(k)["blocked"]


def test_unlabelled_provider_core_and_turtles_are_kept_and_named():
    k = FakeKube(managed=("helm", "harvester"), extra_unlabelled=True)
    plan = cs.uninstall_plan(k)
    assert [p["key"] for p in plan["providers"]] == ["helm", "harvester"]
    kept = {x["provider"]: x["reason"] for x in plan["kept"]}
    assert kept == {"turtles": "turtles", "core/cluster-api": "core", "infrastructure/kubevirt": "not-managed"}
    assert all(p["type"] != "core" for p in plan["providers"])


def test_without_turtles_the_plan_fails_closed():
    with pytest.raises(KubeError):
        cs.uninstall_plan(FakeKube(turtles=False))


def test_dry_run_lists_and_deletes_nothing():
    k = FakeKube()
    seen, step = steps()
    plan = cs.uninstall(k, step, dry_run=True)
    assert [p["key"] for p in plan["providers"]] == ORDER and k.deleted == []
    assert sorted(plan["configmaps"]) == sorted(f"{PROVIDERS[x][0]}/v1" for x in ORDER)
    assert plan["shim"] is True and len(plan["images"]) == 4
    assert all("would delete" in m for s, st, m in seen if s in ("providers", "configmaps", "compatibility"))


# ---------------------------------------------------------------------------
# Le retrait
# ---------------------------------------------------------------------------
def test_order_configmaps_shim_and_never_a_crd():
    k = FakeKube()
    c = Clock()
    plan = cs.uninstall(k, steps()[1], now=c.now, sleep=c.sleep)
    assert provider_deletions(k) == ORDER
    kinds = [kd for kd, _, _ in k.deleted]
    last_provider = max(i for i, kd in enumerate(kinds) if kd == cs.K_CAPIPROVIDER)
    assert all(i > last_provider for i, kd in enumerate(kinds) if kd in ("configmaps", "services"))
    assert sorted(n for kd, ns, n in k.deleted if kd == "configmaps") == ["v1"] * 4      # « other » reste
    assert ("services", cs.SHIM_NS, cs.SHIM_NAME) in k.deleted
    assert not [x for x in k.deleted if x[0] in (cs.K_CRD, "namespaces")]
    assert len([x for x in k.objs if x[0] == cs.K_CRD]) == 2 and plan["blocked"] is False


def test_the_shim_stays_while_an_unremoved_caphv_needs_it():
    k = FakeKube(managed=("helm",))
    k.put(cs.K_CAPIPROVIDER, "caphv-system", "harvester",
          {"spec": {"name": "harvester", "type": "infrastructure", "version": "v0.10.1"}})
    assert cs.uninstall_plan(k)["shim"] is False


def test_a_deployment_that_never_goes_stops_everything_after_it():
    k = FakeKube(stuck={"caphv-system"})
    c = Clock()
    with pytest.raises(RuntimeError, match="harvester-controller.*the next providers are untouched"):
        cs.uninstall(k, steps()[1], timeout=60, now=c.now, sleep=c.sleep)
    assert provider_deletions(k) == ["helm", "harvester"]
    assert not [x for x in k.deleted if x[0] in ("configmaps", "services", cs.K_CRD)]
    assert c.t <= 70


# ---------------------------------------------------------------------------
# La ligne de commande
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("harvester_capi_un", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_cli(cli, monkeypatch, capsys, kube, argv, entry=None):
    monkeypatch.setattr(cli, "kube_from", lambda args: (kube, "kc", entry))
    rc = cli.main(["uninstall", "--kubeconfig", "kc"] + argv)
    return rc, capsys.readouterr()


def test_cli_dry_run_json_contract(cli, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    k = FakeKube()
    rc, out = run_cli(cli, monkeypatch, capsys, k, ["--dry-run", "--json"])
    plan = json.loads(out.out)
    assert rc == cli.EXIT_OK and k.deleted == []
    assert set(plan) == {"providers", "configmaps", "shim", "images", "refusals", "kept", "blocked"}
    assert [p["key"] for p in plan["providers"]] == ORDER
    assert set(plan["providers"][0]) == {"key", "namespace", "type", "name", "version"}


def test_cli_refusal_exits_2_and_deletes_nothing(cli, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    k = FakeKube()
    k.put(cs.K_CLUSTER, "web", "web", {})
    rc, out = run_cli(cli, monkeypatch, capsys, k, ["--json"])
    assert rc == cli.EXIT_BLOCKED and k.deleted == [] and json.loads(out.out)["blocked"] is True
    assert "STEP_EVENT|uninstall|error|refused: capi-clusters-present" in out.err


def test_cli_reads_the_deploy_lock_of_the_console(cli, monkeypatch, capsys, tmp_path):
    """Un déroulé tenu par un autre processus (flock) : refus, rien supprimé."""
    import fcntl
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    d = tmp_path / "deploy.d"
    d.mkdir()
    lock = d / "harvlab.lock"
    lock.write_text(json.dumps({"run": "r1r1r1r1r1r1", "plan": "site-a"}) + "\n")
    assert cli.deploy_holder("harvlab") is None                 # fichier présent, verrou libre
    fd = os.open(lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)           # flock : une autre ouverture du fichier est exclue aussi
    try:
        assert cli.deploy_holder("harvlab") == {"run": "r1r1r1r1r1r1", "plan": "site-a"}
        k = FakeKube()
        rc, out = run_cli(cli, monkeypatch, capsys, k, ["--harvester-name", "harvlab", "--json"])
        assert rc == cli.EXIT_BLOCKED and k.deleted == []
        assert json.loads(out.out)["refusals"][0]["code"] == "capi-plan-running"
    finally:
        os.close(fd)
    assert cli.deploy_holder("../etc") is None and cli.deploy_holder("") is None


def test_cli_timeout_exits_1(cli, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    k = FakeKube(stuck={"caaph-system"})
    c = Clock()
    monkeypatch.setattr(cs.time, "time", c.now)
    monkeypatch.setattr(cs.time, "sleep", c.sleep)
    real = cs.uninstall
    monkeypatch.setattr(cs, "uninstall", lambda *a, **kw: real(*a, now=c.now, sleep=c.sleep, **kw))
    rc, out = run_cli(cli, monkeypatch, capsys, k, ["--timeout", "30"])
    assert rc == cli.EXIT_FAIL and provider_deletions(k) == ["helm"]
    assert "STEP_EVENT|providers|error|addon/helm" in out.err


def test_cli_remove_images_needs_the_configuration(cli, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    with pytest.raises(SystemExit):
        run_cli(cli, monkeypatch, capsys, FakeKube(), ["--remove-images"])


def test_cli_remove_images_goes_through_node_images(cli, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(node_images, "remove_on_nodes", lambda entry, names, step, **kw: calls.append(names))
    entry = {"nodes": [{"ip": "172.16.3.11"}], "ssh": {"user": "rancher"}}
    rc, _ = run_cli(cli, monkeypatch, capsys, FakeKube(), ["--remove-images"], entry=entry)
    assert rc == cli.EXIT_OK and sorted(calls[0]) == sorted(f"reg.example/{k}:v1" for k in ORDER)
    calls.clear()
    rc, _ = run_cli(cli, monkeypatch, capsys, FakeKube(), [], entry=entry)        # décoché : rien
    assert rc == cli.EXIT_OK and calls == []


@pytest.mark.parametrize("names,ok", [
    (["ghcr.io/rancher/cluster-api-provider-rke2-bootstrap:v0.25.2"], True),
    (["reg.example/a@sha256:" + "0" * 64], True),
    (["a; rm -rf /"], False),
    (["$(id)"], False),
    (["x y"], False),
])
def test_remove_on_nodes_quotes_and_refuses_odd_references(names, ok):
    seen = []

    class P:
        returncode = 0
    runner = lambda argv, **kw: seen.append(argv) or P()     # noqa: E731
    entry = {"nodes": [{"ip": "172.16.3.11"}, {"ip": "172.16.3.12"}], "ssh": {"user": "rancher", "port": 22}}
    if not ok:
        with pytest.raises(RuntimeError, match="invalid image reference"):
            node_images.remove_on_nodes(entry, names, lambda *a: None, runner=runner)
        assert seen == []
        return
    res = node_images.remove_on_nodes(entry, names, lambda *a: None, runner=runner)
    assert res == {"nodes": 2, "removed": 2} and len(seen) == 2
    assert seen[0][-2] == "rancher@172.16.3.11" and "images rm" in seen[0][-1]


def test_remove_on_nodes_names_the_failing_node():
    class P:
        returncode = 1
    with pytest.raises(RuntimeError, match="^172.16.3.11: image removal failed$"):
        node_images.remove_on_nodes({"nodes": [{"ip": "172.16.3.11"}]}, ["a/b:c"], lambda *a: None,
                                    runner=lambda argv, **kw: P())


# ---------------------------------------------------------------------------
# Les routes
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app as wapp
    wapp.app.config["TESTING"] = True
    kc = tmp_path / "kc"
    kc.write_text("apiVersion: v1\n")
    role = {"r": "admin"}
    monkeypatch.setattr(wapp, "current_role", lambda: role["r"])
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: None)
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": [{"name": "harvlab", "kubeconfig": str(kc)}]})
    monkeypatch.setattr(wapp, "_identity_kubeconfig", lambda k, ident: k)
    monkeypatch.setattr(wapp, "_capi_base", lambda c, sub: (["py", "harvester-capi", sub], None))
    monkeypatch.setattr(wapp, "_deploy_lock_holder", lambda c: None)
    reads = []

    def run_json(cmd, spec=None, timeout=180):
        reads.append(cmd)
        if cmd[2] == "status":
            return ({"turtles": True, "core": {"ready": True}}, 0), None
        return ({"providers": [], "refusals": [], "kept": [], "blocked": False}, 0), None
    monkeypatch.setattr(wapp, "_capi_run_json", run_json)
    actions, legacy = [], []

    class Run:
        id = "u1u1u1u1u1u1"

    def cli_action(cluster, label, cmd, tool, spec=None, **kw):
        actions.append({"cluster": cluster, "label": label, "cmd": cmd, "tool": tool, **kw})
        return Run(), None
    monkeypatch.setattr(wapp, "_cli_action", cli_action)
    monkeypatch.setattr(wapp, "_capi_uninstall_runner", lambda *a, **kw: legacy.append(a))
    yield {"c": wapp.app.test_client(), "wapp": wapp, "actions": actions, "reads": reads, "legacy": legacy,
           "role": role, "mp": monkeypatch}


@pytest.mark.parametrize("body,status,code", [
    ({}, 400, "confirm-name"),
    ({"confirm": "harvla"}, 400, "confirm-name"),
    ({"confirm": "HARVLAB"}, 400, "confirm-name"),
])
def test_route_needs_the_typed_cluster_name(client, body, status, code):
    r = client["c"].post("/api/capi/harvlab/uninstall", json=body)
    assert r.status_code == status and r.get_json()["code"] == code
    assert client["actions"] == [] and client["legacy"] == []


def test_route_launches_the_tool_as_a_tracked_admin_action(client):
    r = client["c"].post("/api/capi/harvlab/uninstall", json={"confirm": "harvlab"})
    assert r.status_code == 201 and r.get_json()["action_id"] == "u1u1u1u1u1u1"
    a = client["actions"][0]
    assert a["label"] == "capi-uninstall:harvlab" and a["tool"] == "harvester-capi"
    assert a["cmd"] == ["py", "harvester-capi", "uninstall"] and client["legacy"] == []

    class R:
        pass
    run = R()
    a["setup"](run)
    assert run.required_role == "admin"
    client["c"].post("/api/capi/harvlab/uninstall", json={"confirm": "harvlab", "remove_images": True,
                                                            "dry_run": True})
    assert client["actions"][1]["cmd"][3:] == ["--dry-run", "--cluster", "harvlab", "--remove-images"]


@pytest.mark.parametrize("holder", ["lock", "here"])
def test_route_refuses_while_a_plan_deploys_on_the_cluster(client, holder):
    wapp = client["wapp"]
    if holder == "lock":
        client["mp"].setattr(wapp, "_deploy_lock_holder", lambda c: {"run": "r2r2r2r2r2r2", "plan": "site-b"})
    else:
        class Busy:
            id = "r3r3r3r3r3r3"
            result = {"plan": "site-c"}
        client["mp"].setattr(wapp, "_deploy_busy", lambda c: Busy())
    r = client["c"].post("/api/capi/harvlab/uninstall", json={"confirm": "harvlab"})
    assert r.status_code == 409 and r.get_json()["code"] == "capi-plan-running" and client["actions"] == []


def test_route_keeps_the_legacy_runner_before_turtles(client):
    client["mp"].setattr(client["wapp"], "_capi_run_json",
                         lambda cmd, spec=None, timeout=180: (({"turtles": False}, 0), None))
    r = client["c"].post("/api/capi/harvlab/uninstall", json={"dry_run": True})
    assert r.status_code == 201 and client["actions"] == []
    for _ in range(100):
        if client["legacy"]:
            break
        time.sleep(0.01)
    assert client["legacy"]


def test_plan_route_reads_the_dry_run_and_is_admin_only(client):
    r = client["c"].get("/api/capi/harvlab/uninstall/plan")
    assert r.status_code == 200 and r.get_json()["blocked"] is False
    assert client["reads"][-1][2:] == ["uninstall", "--dry-run", "--json"]
    for role in ("operator", "viewer"):
        client["role"]["r"] = role
        assert client["c"].get("/api/capi/harvlab/uninstall/plan").status_code == 403
        assert client["c"].post("/api/capi/harvlab/uninstall", json={"confirm": "harvlab"}).status_code == 403


def test_the_write_route_has_a_readable_rate_limit():
    from limits import parse_many
    src = (ROOT / "web" / "app.py").read_text()
    i = src.index("def api_capi_uninstall(")
    parse_many(re.findall(r'@_rate_limit\("([^"]+)"\)', src[i - 200:i])[-1])
