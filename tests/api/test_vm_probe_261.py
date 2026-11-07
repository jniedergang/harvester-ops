"""v2.6.1 : joignabilité d'un registre depuis le réseau des VMs (point 192).

Pod sonde brève (vm_probe) : manifeste au moindre privilège, lecture de son
journal, chaque chemin d'une sonde impossible (raison dite), nettoyage
toujours fait, cache des contrôles ; constats qui remplacent l'avertissement
« vérifié depuis le cluster Harvester seulement » (registries)."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import registries as rg  # noqa: E402
import vm_probe as vp  # noqa: E402
from kube import KubeError  # noqa: E402

IMAGE = "registry.suse.com/bci/bci-busybox@sha256:" + "e" * 64
NAD = {"metadata": {"name": "lab-net", "namespace": "default",
                    "labels": {"network.harvesterhci.io/clusternetwork": "mgmt"}}}


# ---------------------------------------------------------------------------
# Entrées
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("endpoint,want", [
    ("172.16.2.76", ("172.16.2.76", 443)),
    ("172.16.2.76:5443", ("172.16.2.76", 5443)),
    ("harbor.example", ("harbor.example", 443)),
    ("harbor.example:8443", ("harbor.example", 8443)),
    ("https://harbor.example/v2/", ("harbor.example", 443)),
    ("harbor.example:0", ValueError),
    ("harbor.example:70000", ValueError),
    ("harbor.example:x", ValueError),
    ("[fe80::1]:443", ValueError),
    ("bad host", ValueError),
    ("", ValueError),
])
def test_parse_endpoint(endpoint, want):
    if want is ValueError:
        with pytest.raises(ValueError):
            vp.parse_endpoint(endpoint)
    else:
        assert vp.parse_endpoint(endpoint) == want


@pytest.mark.parametrize("network,ok", [
    ("default/lab-net", True), ("lab-net", False), ("/lab-net", False), ("default/", False),
    ("a/b/c", False), (None, False),
])
def test_network_ref(network, ok):
    if ok:
        assert vp.network_ref(network) == ("default", "lab-net")
    else:
        with pytest.raises(ValueError):
            vp.network_ref(network)


@pytest.mark.parametrize("labels,want", [
    ({"network.harvesterhci.io/clusternetwork": "mgmt"}, {"network.harvesterhci.io/mgmt": "true"}),
    ({"network.harvesterhci.io/clusternetwork": "data"}, {"network.harvesterhci.io/data": "true"}),
    ({}, {}),                                      # NAD sans réseau de cluster (kube-ovn)
])
def test_node_selector_follows_the_cluster_network(labels, want):
    assert vp.node_selector({"metadata": {"labels": labels}}) == want


# ---------------------------------------------------------------------------
# Manifeste : moindre privilège, nettoyage par le cluster
# ---------------------------------------------------------------------------
def job():
    return vp.job_manifest("probe-1", "default/lab-net", NAD, IMAGE, [("172.16.2.76", 443), ("h.example", 8443)], 120)


def test_the_probe_job_has_only_the_capabilities_the_spike_proved():
    pod = job()["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert c["securityContext"]["capabilities"] == {"drop": ["ALL"], "add": ["NET_ADMIN", "NET_RAW"]}
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["securityContext"]["privileged"] is False
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod["automountServiceAccountToken"] is False and pod["enableServiceLinks"] is False
    assert pod["securityContext"] == {"seccompProfile": {"type": "RuntimeDefault"}}
    assert "hostNetwork" not in pod and "hostPID" not in pod
    assert c["volumeMounts"] == [{"name": "tmp", "mountPath": "/tmp"}]
    assert pod["volumes"] == [{"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}}]
    assert c["resources"]["limits"] == {"memory": "32Mi"}


def test_the_probe_job_cleans_itself_and_runs_in_its_own_namespace():
    j = job()
    assert j["metadata"]["namespace"] == "harvester-ops-probe"
    assert j["spec"]["backoffLimit"] == 0
    assert j["spec"]["activeDeadlineSeconds"] == 120
    assert j["spec"]["ttlSecondsAfterFinished"] == vp.TTL_AFTER
    assert j["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    ann = json.loads(j["spec"]["template"]["metadata"]["annotations"][vp.NETWORKS_ANNOTATION])
    assert ann == [{"namespace": "default", "name": "lab-net", "interface": "net1"}]
    pod = j["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"network.harvesterhci.io/mgmt": "true"}
    c = pod["containers"][0]
    assert c["image"] == IMAGE and c["imagePullPolicy"] == "IfNotPresent"
    env = {e["name"]: e["value"] for e in c["env"]}
    assert env == {"TARGETS": "172.16.2.76:443 h.example:8443", "DHCP_WAIT": str(vp.DHCP_WAIT)}
    assert vp.namespace_manifest()["metadata"] == {"name": "harvester-ops-probe",
                                                   "labels": {"harvester-ops.io/managed": "true"}}


def test_the_script_releases_its_lease_and_never_takes_the_default_route():
    s = vp.SCRIPT
    assert "udhcpc -f -i \"$IF\" -R" in s                 # bail rendu à la sortie
    assert "kill \"$P\"" in s
    assert "trap '" in s and "TERM" in s                 # pod arrêtée : udhcpc rend aussi son bail
    assert "timeout 6 nslookup" in s                     # un DNS mort ne mange pas le délai
    assert "default" not in s                           # eth0 garde la route par défaut
    assert "ssl_client -s 3" in s and '-n "$TLS_SNI"' in s and "/v2/" in s     # le nom part en SNI


# ---------------------------------------------------------------------------
# Journal de la pod
# ---------------------------------------------------------------------------
T2 = [("172.16.2.76", 443), ("h.example", 443)]


@pytest.mark.parametrize("log,address,how,results", [
    ("LEASE|172.16.11.86|dhcp\nT|172.16.2.76|443|dns=skip|route=ok|tcp=ok|tls=ok\n"
     "T|h.example|443|dns=ok|route=ok|tcp=ok|tls=ok\n", "172.16.11.86", "dhcp",
     {"172.16.2.76:443": (True, None), "h.example:443": (True, None)}),
    ("LEASE|10.0.0.5|ipam\nT|172.16.2.76|443|dns=skip|route=fail|tcp=skip|tls=skip\n"
     "T|h.example|443|dns=fail|route=skip|tcp=skip|tls=skip\n", "10.0.0.5", "ipam",
     {"172.16.2.76:443": (False, "route"), "h.example:443": (False, "dns")}),
    ("LEASE|10.0.0.5|dhcp\nT|172.16.2.76|443|dns=skip|route=ok|tcp=fail|tls=skip\n"
     "T|h.example|443|dns=ok|route=ok|tcp=ok|tls=fail\n", "10.0.0.5", "dhcp",
     {"172.16.2.76:443": (False, "tcp"), "h.example:443": (False, "tls")}),
    # IPAM sans passerelle ni DNS : la sonde ne juge pas (revue 2.6.1)
    ("LEASE|10.0.0.5|ipam\nT|172.16.2.76|443|dns=skip|route=nogw|tcp=skip|tls=skip\n"
     "T|h.example|443|dns=nodns|route=skip|tcp=skip|tls=skip\n", "10.0.0.5", "ipam",
     {"172.16.2.76:443": (False, None), "h.example:443": (False, None)}),
    ("NOLEASE|no-address\n", None, None, {}),
    ("garbage\nT|x\n", None, None, {}),
])
def test_parse_log(log, address, how, results):
    got = vp.parse_log(log, T2)
    assert (got["address"], got["how"]) == (address, how)
    assert all(v["unknown"] == ("nogw" in v.values() or "nodns" in v.values()) for v in got["results"].values())
    assert {k: (v["ok"], v["failed"]) for k, v in got["results"].items()} == results


# ---------------------------------------------------------------------------
# Déroulé : chaque chemin, nettoyage toujours fait
# ---------------------------------------------------------------------------
class FakeKube:
    def __init__(self, nad=NAD, pods=None, log="", get_error=None, create_error=None, delete_error=None,
                 events=()):
        self.nad, self.pods, self.log = nad, list(pods or []), log
        self.get_error, self.create_error, self.delete_error = get_error, create_error, delete_error
        self.events = list(events)
        self.applied, self.created, self.deleted, self.lists = [], [], [], 0

    def get(self, kind, ns, name):
        if self.get_error:
            raise KubeError(self.get_error)
        return self.nad if kind.startswith("network-attachment-definitions") else None

    def apply(self, docs):
        self.applied.extend(docs)

    def create(self, obj):
        if self.create_error:
            raise KubeError(self.create_error)
        self.created.append(obj)
        return obj

    def list(self, kind, ns=None, selector=None):
        if kind == "events":
            return self.events
        self.lists += 1
        if not self.pods:
            return []
        return [self.pods.pop(0) if len(self.pods) > 1 else self.pods[0]]

    def logs(self, ns, name, tail=200):
        return self.log

    def delete(self, kind, ns, name, cascade=None):
        self.deleted.append((kind, ns, name, cascade))
        if self.delete_error:
            raise KubeError(self.delete_error)


def pod(phase, waiting=None, msg=""):
    st = {"phase": phase}
    if waiting:
        st["containerStatuses"] = [{"state": {"waiting": {"reason": waiting, "message": msg}}}]
    return {"metadata": {"name": "probe-1-abcde"}, "status": st}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run(kube, endpoints=("172.16.2.76",), network="default/lab-net", image=IMAGE):
    c = Clock()
    return vp.run(kube, network, list(endpoints), image, timeout=30, clock=c, sleep=c.sleep)


OK_LOG = "LEASE|172.16.11.86|dhcp\nT|172.16.2.76|443|dns=skip|route=ok|tcp=ok|tls=ok\n"


def test_a_probe_from_the_vm_network_reaches_the_registry():
    k = FakeKube(pods=[pod("Pending"), pod("Running"), pod("Succeeded")], log=OK_LOG)
    got = run(k)
    assert got["status"] == "probed" and got["address"] == "172.16.11.86" and got["how"] == "dhcp"
    assert got["results"]["172.16.2.76"]["ok"] is True
    assert [d["kind"] for d in k.applied] == ["Namespace"] and k.created[0]["kind"] == "Job"
    assert k.deleted == [("jobs", "harvester-ops-probe", k.created[0]["metadata"]["name"], "background")]


@pytest.mark.parametrize("kube_kw,network,image,reason,created", [
    ({}, None, IMAGE, "no-network", False),
    ({}, "lab-net", IMAGE, "network-missing", False),
    ({}, "default/lab-net", None, "image-missing", False),
    ({"nad": None}, "default/lab-net", IMAGE, "network-missing", False),
    ({"get_error": "Error from server (Forbidden): nad is forbidden"}, "default/lab-net", IMAGE, "forbidden", False),
    ({"create_error": "jobs.batch is forbidden: User cannot create"}, "default/lab-net", IMAGE, "forbidden", False),
    ({"create_error": "admission webhook denied"}, "default/lab-net", IMAGE, "not-started", False),
    ({"pods": [pod("Pending", "ErrImageNeverPull", "image not present")]}, "default/lab-net", IMAGE,
     "image-missing", True),
    ({"pods": [pod("Pending", "ImagePullBackOff")]}, "default/lab-net", IMAGE, "image-missing", True),
    ({"pods": [pod("Pending", "ContainerCreating")],
      "events": [{"involvedObject": {"name": "probe-1-abcde"}, "reason": "FailedCreatePodSandBox",
                  "message": "multus: cannot find NAD"}]}, "default/lab-net", IMAGE, "not-started", True),
    ({"pods": []}, "default/lab-net", IMAGE, "not-started", True),
    ({"pods": [pod("Running")]}, "default/lab-net", IMAGE, "timeout", True),
    ({"pods": [pod("Succeeded")], "log": "NOLEASE|no-address\n"}, "default/lab-net", IMAGE, "no-address", True),
    ({"pods": [pod("Succeeded")], "log": "NOLEASE|no-interface\n"}, "default/lab-net", IMAGE, "not-started", True),
])
def test_each_probe_that_cannot_judge_says_why(kube_kw, network, image, reason, created):
    k = FakeKube(**kube_kw)
    got = run(k, network=network, image=image)
    assert got["status"] == "unchecked" and got["reason"] == reason and reason in vp.REASONS
    assert bool(k.created) is created
    # tout Job créé est retiré, quel que soit le chemin
    assert [d[2] for d in k.deleted] == [j["metadata"]["name"] for j in k.created]


def test_a_bad_endpoint_is_a_probe_that_cannot_judge_not_a_traceback():
    k = FakeKube()
    got = run(k, endpoints=["bad host"])
    assert got["status"] == "unchecked" and got["reason"] == "network-missing" and k.created == []


def test_the_sandbox_event_is_the_detail_of_a_probe_that_never_started():
    k = FakeKube(pods=[pod("Pending", "ContainerCreating")],
                 events=[{"involvedObject": {"name": "probe-1-abcde"}, "reason": "FailedCreatePodSandBox",
                          "message": "multus: cannot find NAD"}])
    assert "multus: cannot find NAD" in run(k)["detail"]


def test_a_failed_delete_is_left_to_the_ttl():
    k = FakeKube(pods=[pod("Succeeded")], log=OK_LOG, delete_error="connection refused")
    assert run(k)["status"] == "probed" and len(k.deleted) == 1


def test_a_missing_result_line_counts_as_unreachable():
    k = FakeKube(pods=[pod("Succeeded")], log="LEASE|10.0.0.9|dhcp\n")
    got = run(k)
    assert got["status"] == "probed" and got["results"]["172.16.2.76"] == {"ok": False, "failed": "tcp"}


# ---------------------------------------------------------------------------
# Cache des contrôles
# ---------------------------------------------------------------------------
def test_cached_run_reuses_a_recent_result_and_fresh_forces_a_new_probe(tmp_path):
    calls = []

    def runner(kube, network, endpoints, image):
        calls.append(network)
        return {"network": network, "status": "probed", "results": {}, "n": len(calls)}
    t = {"now": 1000.0}
    now = lambda: t["now"]                                           # noqa: E731
    a = vp.cached_run(tmp_path, "harvlab2", None, "default/lab-net", ["x"], IMAGE, now=now, runner=runner)
    b = vp.cached_run(tmp_path, "harvlab2", None, "default/lab-net", ["x"], IMAGE, now=now, runner=runner)
    assert a["n"] == b["n"] == 1 and b["cached"] is True and "cached" not in a
    vp.cached_run(tmp_path, "harvlab2", None, "default/lab-net", ["x"], IMAGE, fresh=True, now=now, runner=runner)
    vp.cached_run(tmp_path, "harvlab2", None, "default/other", ["x"], IMAGE, now=now, runner=runner)
    vp.cached_run(tmp_path, "harv4", None, "default/lab-net", ["x"], IMAGE, now=now, runner=runner)
    assert len(calls) == 4
    t["now"] += vp.CACHE_SECONDS + 1
    vp.cached_run(tmp_path, "harvlab2", None, "default/lab-net", ["x"], IMAGE, now=now, runner=runner)
    assert len(calls) == 5
    assert oct((tmp_path / vp.CACHE_FILE).stat().st_mode & 0o777) == "0o600"


def test_cached_run_never_keeps_a_probe_that_could_not_judge(tmp_path):
    """Revue 2.6.1 : « non vérifié » dépend souvent des droits de la
    personne ; la suivante doit sonder à nouveau."""
    calls = []

    def runner(kube, network, endpoints, image):
        calls.append(1)
        return {"network": network, "status": "unchecked", "reason": "forbidden"}
    for _ in range(2):
        assert vp.cached_run(tmp_path, "c", None, "default/n", ["x"], IMAGE, runner=runner)["reason"] == "forbidden"
    assert len(calls) == 2


def test_each_key_has_its_own_lock(tmp_path):
    a = vp._lock_path(tmp_path / vp.CACHE_FILE, vp._key("c", "default/a", ["x"]))
    b = vp._lock_path(tmp_path / vp.CACHE_FILE, vp._key("c", "default/b", ["x"]))
    assert a != b and a == vp._lock_path(tmp_path / vp.CACHE_FILE, vp._key("c", "default/a", ["x"]))


# ---------------------------------------------------------------------------
# Constats
# ---------------------------------------------------------------------------
PROBED_OK = {"network": "default/lab-net", "status": "probed", "address": "172.16.11.86",
             "results": {"172.16.2.76": {"ok": True, "failed": None}}}
PROBED_KO = {"network": "default/lab-net", "status": "probed", "address": "172.16.11.86",
             "results": {"172.16.2.76": {"ok": False, "failed": "tcp"}}}
UNCHECKED = {"network": "default/lab-net", "status": "unchecked", "reason": "no-address", "detail": "x",
             "results": {}}


@pytest.mark.parametrize("probe,child,block,code,level,facts", [
    (None, False, False, rg.VM_NETWORK_WARNING, "warn", {"reason": "no-network", "network": ""}),
    (UNCHECKED, False, False, rg.VM_NETWORK_WARNING, "warn", {"reason": "no-address", "network": "default/lab-net"}),
    (PROBED_OK, False, False, rg.VM_NETWORK_OK, "ok", {"address": "172.16.11.86"}),
    (PROBED_KO, False, False, rg.VM_NETWORK_UNREACHABLE, "warn", {"failed": "tcp"}),
    (None, True, False, rg.CHILD_VM_NETWORK_WARNING, "warn", {"reason": "no-network"}),
    (PROBED_OK, True, True, rg.CHILD_VM_NETWORK_OK, "ok", {"network": "default/lab-net"}),
    (PROBED_KO, True, False, rg.CHILD_VM_NETWORK_UNREACHABLE, "warn", {"failed": "tcp"}),
    # repli vers Internet coupé : injoignable = bloquant
    (PROBED_KO, True, True, rg.CHILD_VM_NETWORK_UNREACHABLE, "block", {"failed": "tcp"}),
    # IPAM sans passerelle ni DNS : jamais un faux « injoignable », même repli coupé
    (dict(PROBED_KO, results={"172.16.2.76": {"ok": False, "failed": None, "unknown": True}}), True, True,
     rg.CHILD_VM_NETWORK_WARNING, "warn", {"reason": "ipam-incomplete"}),
])
def test_vm_network_findings(probe, child, block, code, level, facts):
    (f,) = rg.vm_network_findings("amorcage", "172.16.2.76", probe, child=child, block=block)
    assert (f["code"], f["level"]) == (code, level)
    assert f["facts"]["registry"] == "amorcage" and f["facts"]["endpoint"] == "172.16.2.76"
    assert all(f["facts"][k] == v for k, v in facts.items())


def decl(name, **kw):
    d = {"name": name, "kind": "external", "endpoint": "172.16.2.76", "usage": ["images", "charts"],
         "mirrors": ["ghcr.io", "docker.io"], "clusters": ["harvlab2"]}
    d.update(kw)
    return d


@pytest.mark.parametrize("entries,probe,want", [
    (["a"], PROBED_OK, [(rg.CHILD_VM_NETWORK_OK, "ok")]),
    (["a"], PROBED_KO, [(rg.CHILD_VM_NETWORK_UNREACHABLE, "warn")]),
    ([{"registry": "a", "mirrors": ["ghcr.io"], "no_fallback": True}], PROBED_KO,
     [(rg.CHILD_VM_NETWORK_UNREACHABLE, "block")]),
    (["a"], None, [(rg.CHILD_VM_NETWORK_WARNING, "warn")]),
])
def test_child_findings_carry_the_probe(entries, probe, want):
    got = rg.check_child(entries, {"a": decl("a")}, "harvlab2", {"a": True}, (), vm_probe=probe)
    assert [(f["code"], f["level"]) for f in got if f["code"] in rg.CHILD_VM_NETWORK_CODES] == want


@pytest.mark.parametrize("entries,decls,want", [
    (["a", "b"], {"a": decl("a"), "b": decl("b", endpoint="h.example:8443")}, ["172.16.2.76", "h.example:8443"]),
    (["a", "b"], {"a": decl("a"), "b": decl("b")}, ["172.16.2.76"]),               # même adresse : une fois
    (["a"], {"a": decl("a", usage=["charts"])}, []),                               # charts : tirés par CAAPH
    (["x"], {}, []),
])
def test_child_probe_endpoints(entries, decls, want):
    assert rg.child_probe_endpoints(entries, decls) == want


@pytest.mark.parametrize("facts,codes", [
    ({"attached": True, "auth": {"pull": True, "push": True}, "vm_probe": PROBED_OK},
     [rg.VM_NETWORK_OK]),
    ({"attached": True, "auth": {"pull": True, "push": True}, "vm_probe": PROBED_KO},
     [rg.VM_NETWORK_UNREACHABLE]),
    ({"attached": True, "auth": {"pull": True, "push": True}, "write_open": True},
     ["registry-write-port-open", rg.VM_NETWORK_WARNING]),
    ({"attached": False, "vm_probe": PROBED_OK}, ["registry-not-attached"]),
])
def test_registry_check_carries_the_probe_and_the_write_port(facts, codes):
    assert [f["code"] for f in rg.check(decl("a"), facts)] == codes


# ---------------------------------------------------------------------------
# Contrôle d'un cluster enfant (harvester-capi) : la sonde part du réseau
# des VMs de la demande
# ---------------------------------------------------------------------------
sys.path.insert(0, str(ROOT / "tests" / "api"))
from test_capi_registries_260 import Kube, cli, demand, state  # noqa: E402,F401


@pytest.mark.parametrize("regs,network,probed,code", [
    (["amorcage"], "default/production", True, rg.CHILD_VM_NETWORK_OK),
    (["amorcage"], None, False, rg.CHILD_VM_NETWORK_WARNING),          # pas de réseau : non vérifié
    ([], "default/production", False, None),                            # aucun registre : rien
])
def test_child_check_probes_from_the_network_of_the_request(cli, state, monkeypatch, regs, network, probed, code):
    seen = []

    def probe(st, harvester, kube, net, endpoints, image):
        seen.append((harvester, net, endpoints))
        return {"network": net, "status": "probed", "address": "172.16.11.9",
                "results": {e: {"ok": True, "failed": None} for e in endpoints}}
    monkeypatch.setattr(cli, "VM_PROBE", probe)
    spec = demand(registries=regs)
    spec["network"] = network
    got = cli.registry_findings(Kube(), spec, "harvlab2")
    assert seen == ([("harvlab2", network, ["172.16.2.76"])] if probed else [])
    assert [f["code"] for f in got if f["code"] in rg.CHILD_VM_NETWORK_CODES] == ([code] if code else [])
