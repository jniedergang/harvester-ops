"""2.5.0 (décision 169) : contrôle de N clusters ensemble (`check_many`).

La chaîne d'un plan tourne sans surveillance : capacité (CPU, mémoire,
stockage Longhorn cumulé, répliques), adresses des pools et droits de la
personne bloquent avant toute écriture ; dans le formulaire, les manques de
`check` restent des avertissements. Cas à plusieurs clusters, et relevé réel
de harv1 (lecture seule, 05/11/2026) en jeu d'essai.
"""

import argparse
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import capi_cluster as cc  # noqa: E402
import capi_identity as ci  # noqa: E402

GIB = 1024 ** 3
FIX = ROOT / "tests" / "fixtures" / "capi"


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("harvester_capi_fleet", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def lh_node(name, gib=1000, schedulable=True):
    cond = [{"type": "Ready", "status": "True"},
            {"type": "Schedulable", "status": "True" if schedulable else "False"}]
    return {"metadata": {"name": name}, "spec": {"disks": {"d1": {"allowScheduling": True}}},
            "status": {"diskStatus": {"d1": {"conditions": cond, "storageMaximum": gib * GIB,
                                             "storageScheduled": 0, "storageAvailable": gib * GIB}}}}


def pool(name="pool-a", size=16, allocated=0, counter=None):
    p = {"metadata": {"name": name},
         "spec": {"ranges": [{"subnet": "172.16.0.0/16", "gateway": "172.16.0.1",
                              "rangeStart": "172.16.3.1", "rangeEnd": f"172.16.3.{size}"}]},
         "status": {"total": size,
                    "allocated": {f"172.16.3.{i + 1}": f"x/m{i}" for i in range(allocated)}}}
    if counter is not None:
        p["status"]["available"] = counter              # compteur de Harvester, qui dérive
    return cc.pool_info(p)


def facts(cpu=100_000, mem=1000 * GIB, nodes=None, pools=None, denied=None, replicas=1):
    f = {
        "stack": {"ready": True}, "namespaces": [], "clusters": [],
        "images": {"default/leap": {"ready": True, "iso": False, "os": "opensuse", "storage_class": "lh-leap"}},
        "keypairs": ["default/ju"], "networks": ["default/production"],
        "storage_classes": ["harv-rep1"], "pools": {p["name"]: p for p in (pools or [pool()])},
        "versions": [], "free_cpu_m": cpu, "free_memory": mem,
        "overcommit": {"cpu": 1600, "memory": 150},
        "class_replicas": {"lh-leap": replicas, "harv-rep1": 1, "longhorn": 3},
        "longhorn": {"nodes": nodes if nodes is not None else [lh_node("n1")], "over": 100, "minimal": 0},
    }
    if denied is not ...:
        f["denied"] = denied if denied is not None else []
    return f


def demand(name, **kw):
    base = {"name": name, "k8s_version": "v1.34.11", "image": "default/leap", "ssh_keypair": "default/ju",
            "network": "default/production", "gateway": "172.16.0.1", "subnet_mask": "255.255.0.0",
            "ip_pool": "pool-a", "dns": ["172.16.3.6"], "disk_size": "40Gi"}
    base.update(kw)
    return ci.force_target_namespace(cc.normalize(base), "restricted")


def fleet_codes(findings):
    return sorted(f["code"] for f in findings if f["code"].startswith(("fleet-", "rights-"))
                  and f["level"] == "block")


def three(**kw):
    return [demand(f"c{i}", **kw) for i in (1, 2, 3)]


# requests_needed d'un 1+1 de 2 vCPU / 4 Gio à 1600 % et 150 % : 250 m, ~6 Gio
ONE_CPU, ONE_MEM = cc.requests_needed(demand("x"), {"cpu": 1600, "memory": 150})


@pytest.mark.parametrize("label,specs,f,expected", [
    ("three that fit", three(), facts(), []),
    ("third short of memory", three(), facts(mem=ONE_MEM * 2 + 1), ["fleet-memory-short"]),
    ("third short of cpu", three(), facts(cpu=ONE_CPU * 2 + 1), ["fleet-cpu-short"]),
    # 3 clusters x 2 VMs x 40 Gio = 240 Gio
    ("storage, one node", three(), facts(nodes=[lh_node("n1", 200)]), ["fleet-storage-short"]),
    ("storage, two nodes", three(), facts(nodes=[lh_node("n1", 100), lh_node("n2", 100)]),
     ["fleet-storage-short"]),
    ("storage, two nodes enough", three(), facts(nodes=[lh_node("n1", 130), lh_node("n2", 130)]), []),
    ("three replicas, three nodes, one closed", [demand("c1")],
     facts(nodes=[lh_node("n1"), lh_node("n2"), lh_node("n3", schedulable=False)], replicas=3),
     ["fleet-replicas-nodes", "fleet-storage-short"]),
    ("three replicas on one node", [demand("c1")], facts(replicas=3),
     ["fleet-replicas-nodes", "fleet-storage-short"]),
    # un 1+1 demande 3 adresses : 2 clusters = 6 > 5 ; un seul passe
    ("two exceed the pool together", [demand("c1"), demand("c2")], facts(pools=[pool(size=5)]),
     ["fleet-pool-short"]),
    ("one alone fits the pool", [demand("c1")], facts(pools=[pool(size=5)]), []),
    ("same name twice", [demand("c1"), demand("c1")], facts(), ["fleet-name-twice"]),
    ("valid one by one, not together (same namespace)",
     [demand("c1", namespace="shared", target_namespace="shared"),
      demand("c2", namespace="shared", target_namespace="shared")], facts(), ["fleet-name-twice"]),
    ("rights missing", [demand("c1")],
     facts(denied=[(None, "bind", "rbac.authorization.k8s.io", "clusterroles", "harvesterhci.io:cloudprovider")]),
     ["rights-missing"]),
])
def test_fleet_findings(label, specs, f, expected):
    assert fleet_codes(cc.check_many(specs, f)) == expected, label


def test_the_harvester_counter_is_ignored_for_pools():
    # cas réel de harv1 : `available` à 24 pour un pool de 16 adresses, dont 14 allouées
    p = pool(size=16, allocated=14, counter=24)
    assert p["available"] == 2
    got = cc.check_many([demand("c1")], facts(pools=[p]))
    assert fleet_codes(got) == ["fleet-pool-short"]
    short = next(x for x in got if x["code"] == "fleet-pool-short")
    assert short["facts"] == {"pool": "pool-a", "needed": 3, "available": 2}


def test_multi_pool_clusters_fill_their_pools_in_order():
    pools = [pool("pool-a", size=4), pool("pool-b", size=4)]
    specs = [demand("c1", ip_pool="pool-a", ip_pool_refs=["pool-a", "pool-b"]),
             demand("c2", ip_pool="pool-b")]
    # c2 prend 3 de pool-b ; c1 prend 3 de pool-a : tout tient
    assert fleet_codes(cc.check_many(specs, facts(pools=pools))) == []
    specs.append(demand("c3", ip_pool="pool-a", ip_pool_refs=["pool-a", "pool-b"]))
    assert fleet_codes(cc.check_many(specs, facts(pools=pools))) == ["fleet-pool-short"]


def test_per_cluster_rules_stay_and_carry_the_cluster():
    got = cc.check_many([demand("c1"), demand("c2", image="default/absent")], facts())
    missing = [f for f in got if f["code"] == "image-missing"]
    assert [f["cluster"] for f in missing] == ["c2/c2"]


def test_form_shortages_stay_warnings_in_check():
    f = facts(mem=ONE_MEM // 2)
    single = [x for x in cc.check(demand("c1"), f) if x["code"] == "memory-short"]
    assert single and single[0]["level"] == "warn"
    assert "fleet-memory-short" in fleet_codes(cc.check_many([demand("c1")], f))


def test_sizes_and_leases_are_told():
    got = {f["code"]: f for f in cc.check_many(three(), facts(), extra_addresses={"pool-a": 2})}
    assert got["fleet-sizes"]["facts"] == {"clusters": 3, "vms": 6, "vcpu": 12, "memory": 24 * GIB,
                                           "disks": 240 * GIB, "addresses": 11}
    assert got["fleet-dhcp-leases"]["facts"] == {"leases": 3} and got["fleet-dhcp-leases"]["level"] == "ok"


def test_extra_addresses_count_against_their_pool():
    assert fleet_codes(cc.check_many([demand("c1")], facts(pools=[pool(size=4)]),
                                     extra_addresses={"pool-a": 2})) == ["fleet-pool-short"]


@pytest.mark.parametrize("denied,code", [(None, "rights-unchecked"), ([], None)])
def test_rights_unreadable_is_a_warning(denied, code):
    f = facts()
    f["denied"] = denied
    codes = [x["code"] for x in cc.check_many([demand("c1")], f) if x["code"].startswith("rights-")]
    assert codes == ([code] if code else [])


def test_extra_disk_counts_with_its_own_class():
    specs = [demand("c1", extra_disk_size="100Gi", extra_disk_class="longhorn")]
    got = cc.check_many(specs, facts(nodes=[lh_node("n1"), lh_node("n2")]))
    assert fleet_codes(got) == ["fleet-replicas-nodes", "fleet-storage-short"]
    short = next(x for x in got if x["code"] == "fleet-storage-short")
    assert short["facts"]["storage_class"] == "longhorn"


def test_manifest_reviews_cover_namespace_and_rendered_kinds():
    rv = cc.manifest_reviews(demand("c1"))
    assert (None, "create", "", "namespaces", None) in rv
    assert ("c1", "patch", "cluster.x-k8s.io", "clusters", None) in rv
    assert ("c1", "create", "addons.cluster.x-k8s.io", "clusterresourcesets", None) in rv


# ---------------------------------------------------------------------------
# Relevé réel de harv1 (lecture seule, 05/11/2026)
# ---------------------------------------------------------------------------

def harv1(cli):
    inv = json.loads((FIX / "harv1-inventory-2026-11-05.json").read_text())
    f = cli.facts_from(inv)
    f["denied"] = []
    return f


def harv1_demand(name):
    return demand(name, image="default/opensuse-leap-cloud", ip_pool="capi-vm-pool",
                  ssh_keypair=None, disk_size="20Gi")


def test_harv1_one_cluster_passes_and_six_do_not(cli):
    f = harv1(cli)
    assert f["images"]["default/opensuse-leap-cloud"]["storage_class"] == "longhorn-opensuse-leap-cloud"
    one = fleet_codes(cc.check_many([harv1_demand("k1")], f))
    assert one == []
    six = fleet_codes(cc.check_many([harv1_demand(f"k{i}") for i in range(1, 7)], f))
    # 6 x 250 m > 938 m libres ; 6 x 3 adresses > 16 places du pool
    assert "fleet-cpu-short" in six and "fleet-pool-short" in six


# ---------------------------------------------------------------------------
# harvester-capi check --many (une lecture, droits par SelfSubjectAccessReview)
# ---------------------------------------------------------------------------

class Kube:
    def __init__(self, deny=()):
        self.deny = set(deny)
        self.created = []

    def create(self, obj, dry_run=False):
        self.created.append(obj)
        items = obj["items"]
        return {"kind": "List", "items": [
            {"status": {"allowed": (i["spec"]["resourceAttributes"]["verb"],
                                    i["spec"]["resourceAttributes"]["resource"]) not in self.deny}}
            for i in items]}

    def get(self, kind, ns, name):
        return None


def test_check_many_reads_once_and_checks_rights(cli, monkeypatch, tmp_path, capsys):
    reads = []
    inv = json.loads((FIX / "harv1-inventory-2026-11-05.json").read_text())
    monkeypatch.setattr(cli, "inventory", lambda kube, desc: reads.append(1) or copy.deepcopy(inv))
    kube = Kube(deny={("bind", "clusterroles")})
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    monkeypatch.setattr(cli, "components_from", lambda p: None)
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps([{"name": n, "k8s_version": "v1.34.11", "image": "default/opensuse-leap-cloud",
                                 "network": "default/production", "gateway": "172.16.0.1",
                                 "subnet_mask": "255.255.0.0", "ip_pool": "capi-vm-pool",
                                 "dns": ["172.16.3.6"], "disk_size": "20Gi"} for n in ("k1", "k2")]))
    rc = cli.cmd_check(argparse.Namespace(spec=str(spec), many=True, identity="restricted", json=True,
                                          components=None, cluster=None, kubeconfig=None))
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_BLOCKED and out["blocked"] and reads == [1]
    assert len(kube.created) == 1, "one SelfSubjectAccessReview request for all accesses"
    missing = next(f for f in out["findings"] if f["code"] == "rights-missing")
    assert "bind clusterroles.rbac.authorization.k8s.io/harvesterhci.io:cloudprovider" in missing["facts"]["denied"]
    assert out["amount"]["vms"] == 4 and out["caphv_version"] == "v0.10.1"


@pytest.mark.parametrize("body", ["{}", "[]", "[{\"owner\": 1}]"])
def test_check_many_refuses_a_bad_list(cli, monkeypatch, tmp_path, capsys, body):
    monkeypatch.setattr(cli, "kube_from", lambda a: (Kube(), None, None))
    spec = tmp_path / "s.json"
    spec.write_text(body)
    rc = cli.cmd_check(argparse.Namespace(spec=str(spec), many=True, identity="restricted", json=True,
                                          components=None, cluster=None, kubeconfig=None))
    assert rc == cli.EXIT_BLOCKED and json.loads(capsys.readouterr().out)["blocked"]


def test_rights_denied_unreadable_is_none(cli):
    class Bad:
        def create(self, obj, dry_run=False):
            raise cli.KubeError("forbidden")
    assert cli.rights_denied(Bad(), [(None, "get", "", "nodes", None)]) is None
    assert cli.rights_denied(Bad(), []) == []
