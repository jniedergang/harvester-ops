"""2.10.1 tâche 3 : adresse de Harvester et repli `ingress-expose` en 1.9.1.

CAPHV v0.10.1 lit l'adresse de Harvester dans `kube-system/ingress-expose`.
Une mise à jour en 1.9.1 d'un cluster venu de 1.8 peut retirer ce service
APRÈS l'installation de la pile : rien ne le recréait, CAPHV échouait en
boucle sur les clusters existants. `shim_drift` le dit (clusters touchés
nommés), `repair_shim` le repose par une action suivie (point 321 B : jamais
de pose silencieuse par une tâche de fond).

Hypothèse à confirmer au banc (tâche 1, essais 5 et 8) : sur harvlab en 1.9.1
(venu de 1.8), `ingress-expose` d'origine a disparu et `rke2-traefik` porte la
VIP dans son statut ; un `ingress-expose` reposé par Harvester se reconnaît à
un gestionnaire `managedFields` autre que la console (`harvester-ops`), hors
sous-ressource `status`.

Mutations rapportées (rapport 2.10.1, tâche 3) : « nécessaire seulement à
l'installation » (shim_drift rendu par shim_state) -> le cas 1.9.1 échoue ;
« réparation par une tâche de fond » (stack_status qui applique) ->
`test_status_never_writes` échoue ; « gestionnaire étranger ignoré » -> le cas
foreign échoue.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).parent))
import capi_stack as cs  # noqa: E402
from test_capi_stack import FakeKube, desc, steps  # noqa: E402

NATIVE = {"metadata": {"annotations": {cs.VIP_ANNOTATION: "172.16.2.70"}},
          "spec": {"type": "LoadBalancer"}, "status": {"loadBalancer": {"ingress": [{"ip": "172.16.2.70"}]}}}
OURS = {"metadata": {"labels": {cs.SHIM_LABEL: "caphv-ingress-expose", cs.MANAGED: "true"},
                     "annotations": {cs.VIP_ANNOTATION: "172.16.3.100"},
                     "managedFields": [{"manager": "harvester-ops", "operation": "Apply"}]}}
# notre service, puis Harvester qui repose le sien par-dessus (même nom)
FOREIGN = {"metadata": {"labels": {cs.SHIM_LABEL: "caphv-ingress-expose"},
                        "annotations": {cs.VIP_ANNOTATION: "172.16.3.100"},
                        "managedFields": [{"manager": "harvester-ops", "operation": "Apply"},
                                          {"manager": "helm", "operation": "Update",
                                           "fieldsV1": {"f:metadata": {"f:labels": {}}, "f:spec": {"f:ports": {}}}},
                                          {"manager": "kube-vip", "operation": "Update", "subresource": "status"}]}}
STATUS_ONLY = {"metadata": {"labels": {cs.SHIM_LABEL: "caphv-ingress-expose"},
                            "annotations": {cs.VIP_ANNOTATION: "172.16.3.100"},
                            "managedFields": [{"manager": "harvester-ops", "operation": "Apply"},
                                              {"manager": "kube-vip", "operation": "Update",
                                               "subresource": "status"}]}}

# 2.10.1, relevé sur harv1 (1.9.1) le 27/11/2026 : kube-ovn-controller annote
# tout service à adresse de cluster (`ovn.kubernetes.io/vpc`), kube-vip pose
# `kube-vip.io/vipHost` ; ces écritures d'annotations seules ne reprennent
# pas le service (shim recréé en ClusterIP normal par la session CAPHV le 12/10)
ANNOTATED = {"metadata": {"labels": {cs.SHIM_LABEL: "caphv-ingress-expose"},
                          "annotations": {cs.VIP_ANNOTATION: "172.16.3.100"},
                          "managedFields": [
                              {"manager": "harvester-ops", "operation": "Apply"},
                              {"manager": "kube-ovn-controller", "operation": "Update",
                               "fieldsV1": {"f:metadata": {"f:annotations": {"f:ovn.kubernetes.io/vpc": {}}}}},
                              {"manager": "kube-vip", "operation": "Update",
                               "fieldsV1": {"f:metadata": {"f:annotations": {"f:kube-vip.io/vipHost": {}}}}}]}}


def installed(kube, version="v0.10.1"):
    kube.put(cs.K_CAPIPROVIDER, "caphv-system", "harvester", {
        "spec": {"name": "harvester", "type": "infrastructure", "version": version},
        "status": {"phase": "Ready", "installedVersion": version, "conditions": [{"type": "Ready", "status": "True"}]}})


def clusters(kube, *names):
    for n in names:
        ns, name = n.split("/")
        kube.put(cs.K_CLUSTER, ns, name, {"spec": {"infrastructureRef": {"kind": "HarvesterCluster", "name": name}}})
    kube.put(cs.K_CLUSTER, "fleet-local", "local", {"spec": {}})                    # Rancher, pas CAPHV
    kube.put(cs.K_CLUSTER, "other", "aws", {"spec": {"infrastructureRef": {"kind": "AWSCluster", "name": "x"}}})


def _no_workaround(d):
    for p in d["providers"]:
        if p["name"] == "harvester":
            p["workarounds"] = []
            p["version"] = "v0.11.0"
    return d


@pytest.mark.parametrize("label,traefik,svc,caphv,bundle,want", [
    ("fresh 1.9 (rke2-traefik only), console shim", "172.16.3.100", OURS, "v0.10.1", None,
     {"needed": True, "present": True, "ours": True, "foreign": False, "repair": False, "clusters_at_risk": []}),
    ("1.9.0 from 1.8, native ingress-expose", None, NATIVE, "v0.10.1", None,
     {"needed": False, "present": True, "ours": False, "foreign": False, "repair": False, "clusters_at_risk": []}),
    ("1.9.1 from 1.8, ingress-expose removed by the upgrade", "172.16.2.60", None, "v0.10.1", None,
     {"needed": True, "present": False, "ours": False, "foreign": False, "repair": True, "vip": "172.16.2.60",
      "clusters_at_risk": ["apps/a", "web/b"]}),
    ("ingress-expose removed, no VIP anywhere", None, None, "v0.10.1", None,
     {"needed": True, "present": False, "repair": False, "vip": None, "clusters_at_risk": ["apps/a", "web/b"]}),
    ("console shim then Harvester's own service back", "172.16.3.100", FOREIGN, "v0.10.1", None,
     {"needed": False, "present": True, "ours": True, "foreign": True, "repair": False, "clusters_at_risk": []}),
    ("kube-vip writing the status only is not foreign", "172.16.3.100", STATUS_ONLY, "v0.10.1", None,
     {"needed": True, "present": True, "foreign": False, "repair": False}),
    ("annotation-only writers (kube-ovn, kube-vip) are not foreign", "172.16.3.100", ANNOTATED, "v0.10.1", None,
     {"needed": True, "present": True, "ours": True, "foreign": False, "managers": []}),
    ("no CAPHV installed", "172.16.3.100", None, None, None,
     {"needed": False, "present": False, "repair": False, "clusters_at_risk": []}),
    ("CAPHV without the workaround", "172.16.3.100", None, "v0.11.0", _no_workaround,
     {"needed": False, "present": False, "repair": False, "clusters_at_risk": []}),
    ("CAPHV installed in another version than the bundle", "172.16.3.100", None, "v0.9.0", None,
     {"needed": False, "present": False, "repair": False}),
])
def test_shim_drift(tmp_path, label, traefik, svc, caphv, bundle, want):
    d = desc(tmp_path)
    if bundle:
        d = bundle(d)
    kube = FakeKube(traefik_ip=traefik, ingress_expose=svc)
    if caphv:
        installed(kube, caphv)
    clusters(kube, "apps/a", "web/b")
    got = cs.shim_drift(kube, d["providers"], kube.list(cs.K_CLUSTER))
    for k, v in want.items():
        assert got[k] == v, (label, k, got)


def test_status_says_the_drift_and_never_writes(tmp_path):
    d = desc(tmp_path)
    kube = FakeKube(traefik_ip="172.16.2.60")
    for p in d["providers"]:
        installed(kube, p["version"]) if p["name"] == "harvester" else None
    clusters(kube, "apps/a")
    st = cs.stack_status(kube, d["providers"])
    assert st["shim_drift"]["repair"] is True and st["shim_drift"]["clusters_at_risk"] == ["apps/a"]
    assert "compatibility/ingress-expose" in st["missing"]
    assert kube.applied == [] and kube.get("services", "kube-system", "ingress-expose") is None


def test_status_foreign_is_said_without_repair(tmp_path):
    d = desc(tmp_path)
    kube = FakeKube(ingress_expose=FOREIGN)
    installed(kube)
    st = cs.stack_status(kube, d["providers"])
    assert st["shim_drift"]["foreign"] is True and st["shim_drift"]["repair"] is False
    assert kube.applied == []


def test_repair_poses_the_shim_and_names_the_clusters(tmp_path):
    d = desc(tmp_path)
    kube = FakeKube(traefik_ip="172.16.2.60")
    installed(kube)
    clusters(kube, "apps/a", "web/b")
    seen, step = steps()
    out = cs.repair_shim(kube, d["providers"], step)
    shim = kube.get("services", "kube-system", "ingress-expose")
    assert shim["metadata"]["annotations"][cs.VIP_ANNOTATION] == "172.16.2.60"
    assert shim["metadata"]["labels"][cs.SHIM_LABEL] == "caphv-ingress-expose"
    assert out["present"] and out["ours"] and not out["repair"]
    assert ("compatibility", "done",
            "ingress-expose recreated for CAPHV (VIP 172.16.2.60); clusters served again: apps/a, web/b") in seen


@pytest.mark.parametrize("label,traefik,svc,caphv,err", [
    ("foreign service", "172.16.3.100", FOREIGN, "v0.10.1", "ingress-expose-foreign"),
    ("no VIP", None, None, "v0.10.1", "cannot find the Harvester VIP"),
])
def test_repair_refusals(tmp_path, label, traefik, svc, caphv, err):
    d = desc(tmp_path)
    kube = FakeKube(traefik_ip=traefik, ingress_expose=svc)
    installed(kube, caphv)
    with pytest.raises(RuntimeError, match=err):
        cs.repair_shim(kube, d["providers"], steps()[1])
    assert kube.applied == []


@pytest.mark.parametrize("label,svc,caphv", [
    ("already present", OURS, "v0.10.1"),
    ("native service", NATIVE, "v0.10.1"),
    ("no CAPHV", None, None),
])
def test_repair_nothing_to_do(tmp_path, label, svc, caphv):
    d = desc(tmp_path)
    kube = FakeKube(ingress_expose=svc)
    if caphv:
        installed(kube, caphv)
    seen, step = steps()
    cs.repair_shim(kube, d["providers"], step)
    assert kube.applied == []
    assert seen[-1][:2] == ("compatibility", "done") and "nothing to repair" in seen[-1][2]


# ---------------------------------------------------------------------------
# Outil et route : action suivie, administrateur
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cli():
    import importlib.util
    s = importlib.util.spec_from_file_location("harvester_capi_2101", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("label,traefik,svc,rc,last", [
    ("repaired", "172.16.2.60", None, 0, ("compatibility", "done")),
    ("foreign", "172.16.3.100", FOREIGN, 1, ("compatibility", "error")),
    ("no VIP", None, None, 1, ("compatibility", "error")),
])
def test_cli_repair_shim(cli, tmp_path, monkeypatch, label, traefik, svc, rc, last):
    d = desc(tmp_path)
    kube = FakeKube(traefik_ip=traefik, ingress_expose=svc)
    installed(kube)
    seen = []
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, "/x", None))
    monkeypatch.setattr(cli, "components_from", lambda p: d)
    monkeypatch.setattr(cli, "uninstall_refusal", lambda h: None)
    monkeypatch.setattr(cli, "step", lambda sid, st, msg="": seen.append((sid, st, msg)))
    assert cli.main(["repair-shim", "--kubeconfig", "/x", "--components", "/b"]) == rc
    assert seen[-1][:2] == last


def test_cli_status_names_the_clusters_at_risk(cli, tmp_path, monkeypatch, capsys):
    d = desc(tmp_path)
    kube = FakeKube(traefik_ip="172.16.2.60")
    installed(kube)
    clusters(kube, "apps/a")
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, "/x", None))
    monkeypatch.setattr(cli, "components_from", lambda p: d)
    cli.main(["status", "--kubeconfig", "/x", "--components", "/b"])
    assert "ingress-expose missing for CAPHV: run repair-shim (clusters at risk: apps/a)" in capsys.readouterr().out


def test_route_is_admin_only_and_tracked(monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT / "web"))
    import app as wapp
    assert wapp.required_role_for("/api/capi/h1/repair-shim", "POST") == "admin"
    wapp.app.config["TESTING"] = True
    c = wapp.app.test_client()
    monkeypatch.setattr(wapp, "_capi_components_dir", lambda: None)
    assert c.post("/api/capi/h1/repair-shim").status_code == 412
    calls = []
    monkeypatch.setattr(wapp, "_capi_components_dir", lambda: tmp_path)
    monkeypatch.setattr(wapp, "_capi_base", lambda cl, sub: (["harvester-capi", sub, "--cluster", cl], None))
    monkeypatch.setattr(wapp, "_capi_action", lambda cl, label, cmd, **kw: calls.append((label, cmd))
                        or (type("R", (), {"id": "abc123"})(), None))
    r = c.post("/api/capi/h1/repair-shim")
    assert r.status_code == 201 and r.get_json() == {"action_id": "abc123"}
    assert calls == [("capi-repair-shim:h1", ["harvester-capi", "repair-shim", "--cluster", "h1",
                                               "--components", str(tmp_path)])]


# ---------------------------------------------------------------------------
# Disponibilité d'un plan : le contournement manque sans phase capi
# ---------------------------------------------------------------------------
CAPHV_CR = {"metadata": {"namespace": "caphv-system", "name": "harvester"},
            "spec": {"name": "harvester", "type": "infrastructure", "version": "v0.10.1"},
            "status": {"phase": "Ready", "installedVersion": "v0.10.1",
                       "conditions": [{"type": "Ready", "status": "True"}]}}
WANT = [{"name": "harvester", "type": "infrastructure", "version": "v0.10.1", "workarounds": ["ingress-expose"]}]


@pytest.mark.parametrize("label,phases,expose,wanted,code", [
    ("1.9.1 from 1.8, clusters only", ["clusters"], None, WANT, True),
    ("capi phase recreates it itself", ["capi", "clusters"], None, WANT, False),
    ("service present", ["clusters"], OURS, WANT, False),
    ("no bundle read", ["clusters"], None, None, False),
])
def test_deploy_ready_ingress_expose_missing(label, phases, expose, wanted, code):
    import copy
    import deploy_ready as dr
    from test_deploy_plans_250 import reads
    r = reads(ingress_expose=copy.deepcopy(expose), capi_wanted=wanted)
    r["capi_providers"] = r["capi_providers"] + [copy.deepcopy(CAPHV_CR)]
    ev = dr.evaluate(r, phases=phases)
    got = [u for u in ev["unmet"] if u["code"] == "ingress-expose-missing"]
    assert bool(got) is code, (label, ev)
    if code:
        assert "Repair the address for CAPHV" in got[0]["detail"]
        assert "ingress-expose-missing" in dr.CODES


def test_real_kube_reads_the_managed_fields(monkeypatch):
    """kubectl >= 1.21 omet managedFields sans --show-managed-fields (vu sur
    harv1 le 27/11/2026) : sans le drapeau, un service repris par Harvester
    passait pour le nôtre et la règle `foreign` ne voyait jamais rien."""
    import json as _json
    from kube import Kube
    calls = []
    svc = _json.loads(_json.dumps(FOREIGN))
    svc["metadata"]["name"] = "ingress-expose"
    caphv = {"items": [{"metadata": {"name": "harvester", "namespace": "caphv-system"},
                        "spec": {"name": "harvester", "type": "infrastructure", "version": "v0.10.1"},
                        "status": {"phase": "Ready", "installedVersion": "v0.10.1",
                                   "conditions": [{"type": "Ready", "status": "True"}]}}]}

    def run(self, *args, input=None, timeout=None):
        calls.append(args)
        if "ingress-expose" in args:
            o = _json.loads(_json.dumps(svc))
            if "--show-managed-fields" not in args:
                o["metadata"].pop("managedFields")
            return _json.dumps(o)
        if args[1] == cs.K_CAPIPROVIDER:
            return _json.dumps(caphv)
        return _json.dumps({"items": []})
    monkeypatch.setattr(Kube, "run", run)
    got = cs.shim_drift(Kube("/dev/null"), WANT, [])
    assert got["foreign"] is True and got["managers"] == ["helm"], got
    assert any("ingress-expose" in a and "--show-managed-fields" in a for a in calls)


def test_deploy_read_asks_kubectl_for_the_managed_fields():
    src = (ROOT / "web" / "app.py").read_text()
    body = src[src.index("def _deploy_read_capi("):]
    body = body[body.index("def ingress_expose():"):body.index("def providers():")]
    assert '"--show-managed-fields"' in body
