"""v1.48.0 : les décisions de la création de cluster RKE2 (Cluster API).

Tout ce qui se décide sans cluster : ce qu'on accepte (liste blanche), comment
cela devient des options du générateur, ce qu'on corrige dans sa sortie, les
adresses tirées d'un IPPool, le contrôle préalable, et la lecture de la
progression. Les objets de tests/fixtures/capi/ viennent du cluster
d'essai monté sur harv1 le 25/09/2026 (Harvester 1.9.0, cœur CAPI v1.13.3 de
Turtles, CAPHV v0.10.1).
"""

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import capi_cluster as cc  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "capi"


def load(name):
    return json.loads((FIX / name).read_text())


def good(**over):
    raw = {"name": "web", "k8s_version": "v1.31.14", "image": "default/sles15-sp7.qcow2",
           "ssh_keypair": "default/capi-ssh-key", "network": "default/production",
           "gateway": "172.16.0.1", "subnet_mask": "255.255.0.0", "ip_pool": "capi-vm-pool",
           "dns": "172.16.3.6"}
    raw.update(over)
    return cc.normalize(raw)


# -- liste blanche et forme -------------------------------------------------

def test_an_unknown_option_is_refused_before_anything():
    """Le générateur accepte `--apply`, qui appliquerait avec le kubeconfig
    ambiant du service : rien ne doit l'atteindre qui ne soit pas prévu."""
    with pytest.raises(ValueError, match="apply"):
        cc.normalize({"name": "x", "apply": True})


def test_defaults_and_derived_values():
    s = good()
    assert s["namespace"] == "web"            # un espace de noms par cluster
    assert s["cp_replicas"] == 1 and s["worker_replicas"] == 1
    assert s["dns"] == ["172.16.3.6"] and s["rancher_import"] is False
    assert s["target_namespace"] == "default"


def test_a_good_request_is_valid():
    assert cc.validate(good()) == []


@pytest.mark.parametrize("over, key", [
    ({"name": "Web"}, "name"), ({"name": "9web"}, "name"), ({"name": "w" * 41}, "name"),
    ({"namespace": "kube-system"}, "namespace"), ({"namespace": "default"}, "namespace"),
    ({"k8s_version": "1.31"}, "k8s_version"), ({"cp_replicas": 0}, "cp_replicas"),
    ({"memory": "4G"}, "memory"), ({"image": "sles.qcow2"}, "image"),
    ({"gateway": "172.16.0"}, "gateway"), ({"subnet_mask": "255.0.255.0"}, "subnet_mask"),
    ({"dns": ""}, "dns"), ({"dns": "8.8.8.8,nope"}, "dns"), ({"cni": "flannel"}, "cni"),
    ({"pod_cidr": "10.42.0.0/33"}, "pod_cidr"), ({"cni_mtu": 100}, "cni_mtu"),
    ({"fleet_repo": "file:///etc"}, "fleet_repo"),
    ({"extra_disk_size": "20Gi"}, "extra_disk_class"),
    ({"extra_networks": "production"}, "extra_networks"),
])
def test_each_malformed_value_is_named(over, key):
    assert key in [k for k, _ in cc.validate(good(**over))]


# -- options du générateur --------------------------------------------------

def test_the_generator_gets_exactly_the_allowed_flags():
    argv = cc.to_argv(good(worker_replicas=2, extra_disk_size="20Gi",
                           extra_disk_class="harv-rep1", ip_pool_refs="a,b"), "/k/h.yaml")
    assert "--apply" not in argv
    assert argv[argv.index("--name") + 1] == "web"
    assert argv[argv.index("--worker-replicas") + 1] == "2"
    assert argv[argv.index("--extra-disk") + 1] == "20Gi:harv-rep1"
    assert argv[argv.index("--ip-pool-refs") + 1] == "a,b"
    assert argv[argv.index("--dns") + 1] == "172.16.3.6"
    assert argv[-2:] == ["--harvester-kubeconfig", "/k/h.yaml"]
    # ces réglages CNI n'ont d'effet qu'en mode Fleet : on ne les envoie pas
    assert "--cni-mtu" not in argv and "--fleet-addon-branch" not in argv


def test_cni_tuning_goes_with_fleet_mode():
    argv = cc.to_argv(good(fleet_repo="https://git.example/addons.git", cni_mtu=1450),
                      "/k/h.yaml")
    assert argv[argv.index("--fleet-addon-repo") + 1] == "https://git.example/addons.git"
    assert argv[argv.index("--cni-mtu") + 1] == "1450"
    assert argv[argv.index("--fleet-addon-branch") + 1] == "main"


# -- corrections de la sortie du générateur -----------------------------------

def _generated():
    return [
        {"apiVersion": "cluster.x-k8s.io/v1beta1", "kind": "ClusterClass",
         "metadata": {"name": "harvester-rke2", "namespace": "web"},
         "spec": {"infrastructure": {"ref": {"apiVersion": cc.INFRA_OLD,
                                             "kind": "HarvesterClusterTemplate"}},
                  "workers": {"machineDeployments": [{"template": {"infrastructure": {
                      "ref": {"apiVersion": cc.INFRA_OLD, "kind": "HarvesterMachineTemplate"}}}}]}}},
        {"apiVersion": cc.INFRA_OLD, "kind": "HarvesterMachineTemplate",
         "metadata": {"name": "w"}, "spec": {"template": {"spec": {"cpu": 2}}}},
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "hv-identity-secret"},
         "data": {"kubeconfig": "c2VjcmV0"}},
        {"apiVersion": "cluster.x-k8s.io/v1beta1", "kind": "Cluster",
         "metadata": {"name": "web", "labels": {cc.AUTO_IMPORT: "true", "ccm": "external"}},
         "spec": {"topology": {"variables": [
             {"name": "targetNamespace", "value": "default"},
             {"name": "vmNetworks", "value": ["default/production"]}]}}},
        None,
    ]


def test_harvester_objects_and_references_move_to_v1beta1():
    """Vu sur harv1 : en v1alpha1, la conversion ajoutait
    `spec.template.metadata: {}`, que le schéma de CAPHV refuse ensuite."""
    out = cc.postprocess(_generated(), good())
    assert cc.INFRA_OLD not in json.dumps(out)
    assert out[1]["apiVersion"] == cc.INFRA_NEW
    assert out[0]["spec"]["infrastructure"]["ref"]["apiVersion"] == cc.INFRA_NEW
    # le document vide est écarté ; kube-vip s'ajoute (v1.52.0)
    assert len([d for d in out if d["metadata"]["name"] not in (cc.KUBE_VIP_CM, cc.KUBE_VIP_CRS)]) == 4


def test_no_rancher_import_unless_asked():
    out = cc.postprocess(_generated(), good())
    labels = [d for d in out if d["kind"] == "Cluster"][0]["metadata"]["labels"]
    assert cc.AUTO_IMPORT not in labels and labels["ccm"] == "external"
    out = cc.postprocess(_generated(), good(rancher_import=True))
    assert [d for d in out if d["kind"] == "Cluster"][0]["metadata"]["labels"][cc.AUTO_IMPORT] == "true"


def test_console_only_options_reach_the_topology():
    out = cc.postprocess(_generated(), good(target_namespace="capi-vms",
                                            extra_networks="default/storage",
                                            service_cidr="10.96.0.0/16"))
    cl = [d for d in out if d["kind"] == "Cluster"][0]
    vars_ = {v["name"]: v["value"] for v in cl["spec"]["topology"]["variables"]}
    assert vars_["targetNamespace"] == "capi-vms"
    assert vars_["vmNetworks"] == ["default/production", "default/storage"]
    assert cl["spec"]["clusterNetwork"]["services"]["cidrBlocks"] == ["10.96.0.0/16"]


def test_generated_objects_are_labelled_for_the_deletion():
    """Le secret d'identité porte un kubeconfig du cluster Harvester : il ne
    doit pas survivre au cluster (vu dans essai-capi après sa suppression)."""
    out = cc.postprocess(_generated(), good())
    for d in out:
        labelled = (d["metadata"].get("labels") or {}).get(cc.GENERATED) == "true"
        assert labelled == (d["kind"] not in ("Namespace", "Cluster")), d["kind"]
    kinds = {d["kind"].lower() + ("es" if d["kind"].endswith("s") else "s") for d in out}
    known = {k.split(".", 1)[0] for k in cc.GENERATED_KINDS}
    assert kinds - {"clusters"} <= known


def test_a_cluster_in_another_namespace_is_no_neighbour():
    found = cc.check(good(), _facts(clusters=["apps/web"]))
    assert "namespace-has-cluster" not in codes(found)


def test_postprocess_leaves_the_input_untouched():
    src = _generated()
    before = json.dumps(src)
    cc.postprocess(src, good(target_namespace="x"))
    assert json.dumps(src) == before


def test_a_preview_never_shows_the_identity_secret():
    shown = json.dumps(cc.masked(cc.postprocess(_generated(), good())))
    assert "c2VjcmV0" not in shown and "<hidden>" in shown


# -- adresses ------------------------------------------------------------------

def test_the_real_pool_gives_gateway_mask_and_room():
    info = cc.pool_info(load("ippool-capi-vm-pool.json"))
    assert info["gateway"] == "172.16.0.1" and info["mask"] == "255.255.0.0"
    assert info["subnet"] == "172.16.0.0/16"
    assert [r["size"] for r in info["ranges"]] == [6, 10] and info["total"] == 16
    assert info["available"] == 16 - len(info["used"])


def test_a_drifting_available_counter_is_not_believed():
    """Vu sur harv1 le 25/09/2026 : available=24 pour total=16 après la
    suppression de quelques clusters ; la table des allocations fait foi."""
    pool = load("ippool-capi-vm-pool.json")
    pool["status"].update(available=24, total=16)
    pool["status"].pop("allocated", None)
    assert cc.pool_info(pool)["available"] == 16
    pool["status"]["allocated"] = {"172.16.3.44": "a/b", "172.16.3.45": "a/c"}
    pool["status"]["available"] = 99
    assert cc.pool_info(pool)["available"] == 14


def test_addresses_needed_count_the_rollout_spare():
    assert cc.addresses_needed(good(cp_replicas=3, worker_replicas=2)) == 6


@pytest.mark.parametrize("pod, svc, vm, hits", [
    ("10.42.0.0/16", "10.43.0.0/16", "172.16.0.0/16", []),
    ("172.16.0.0/20", "10.43.0.0/16", "172.16.0.0/16", ["pod_cidr"]),
    ("10.42.0.0/15", "10.43.0.0/16", "172.16.0.0/16", ["pod_cidr"]),
])
def test_cidr_overlaps(pod, svc, vm, hits):
    got = [k for k, _, _ in cc.cidr_overlaps(good(pod_cidr=pod, service_cidr=svc), vm)]
    assert got == hits


# -- contrôle préalable --------------------------------------------------------

def _facts(**over):
    f = {"stack": {"ready": True, "missing": [], "turtles": True, "legacy": False},
         "namespaces": [], "clusters": [],
         "images": {"default/sles15-sp7.qcow2": {"ready": True, "iso": False}},
         "keypairs": ["default/capi-ssh-key"], "networks": ["default/production"],
         "storage_classes": ["harv-rep1"],
         "pools": {"capi-vm-pool": cc.pool_info(load("ippool-capi-vm-pool.json"))},
         "versions": ["v1.31.14", "v1.33.5"], "free_cpu_m": 848,
         "free_memory": 40 * 1024 ** 3, "overcommit": {"cpu": 1600, "memory": 150}}
    f.update(over)
    return f


def codes(findings, level=None):
    return [f["code"] for f in findings if level is None or f["level"] == level]


def test_a_good_request_on_a_ready_cluster_has_no_blocker():
    found = cc.check(good(), _facts())
    assert cc.blocking(found) == []
    assert "cp-single" in codes(found, "warn")          # un seul plan de contrôle : dit
    assert "endpoint-dhcp" in codes(found, "ok")


@pytest.mark.parametrize("over, facts, code", [
    ({}, {"stack": {"ready": False, "missing": ["harvester"]}}, "stack-not-ready"),
    ({}, {"clusters": ["web/web"]}, "name-taken"),
    ({}, {"clusters": ["web/other"], "namespaces": ["web"]}, "namespace-has-cluster"),
    ({"image": "default/other.qcow2"}, {}, "image-missing"),
    ({}, {"images": {"default/sles15-sp7.qcow2": {"ready": True, "iso": True}}}, "image-iso"),
    ({}, {"images": {"default/sles15-sp7.qcow2": {"ready": False, "iso": False}}}, "image-not-ready"),
    ({"ssh_keypair": "default/nope"}, {}, "keypair-missing"),
    ({"network": "default/untagged"}, {}, "network-missing"),
    ({"ip_pool": "nope"}, {}, "pool-missing"),
    ({"worker_replicas": 20}, {}, "pool-short"),
    ({"pod_cidr": "172.16.0.0/20"}, {}, "cidr-overlap"),
    ({"name": "Bad"}, {}, "invalid"),
])
def test_each_blocker(over, facts, code):
    assert code in codes(cc.check(good(**over), _facts(**facts)), "block")


@pytest.mark.parametrize("over, facts, code", [
    ({"gateway": "10.0.0.1"}, {}, "gateway-differs"),
    ({"subnet_mask": "255.255.255.0"}, {}, "mask-differs"),
    ({"cp_replicas": 2}, {}, "cp-even"),
    ({"cpu": 1}, {}, "small-nodes"),
    ({"k8s_version": "v1.30.1"}, {}, "version-untested"),
    ({"cni_mtu": 1450}, {}, "cni-tuning-ignored"),
    ({"worker_replicas": 5, "cpu": 4}, {}, "cpu-short"),
    ({"worker_replicas": 12, "memory": "16Gi"}, {}, "memory-short"),
    ({}, {"namespaces": ["web"]}, "namespace-exists"),
    ({}, {"images": {"default/sles15-sp7.qcow2": {"ready": True, "iso": False, "os": "sles"}}},
     "image-sles-repos"),
    ({}, {"stack": {"ready": True, "legacy": True}}, "stack-legacy"),
])
def test_each_warning(over, facts, code):
    found = cc.check(good(**over), _facts(**facts))
    assert code in codes(found, "warn")


# -- progression -----------------------------------------------------------------

def test_requests_follow_harvester_overcommit():
    """Relevé sur harv1 : une VM de 2 vCPU et 4 Gio à 1600 % / 150 % demande
    125 m de CPU et 3 305 109 497 octets. Sans surengagement, elle demande
    tout."""
    cpu, mem = cc.requests_needed(good(worker_replicas=0), {"cpu": 1600, "memory": 150})
    assert cpu == 125
    assert abs(mem - 3_305_109_497) < 100 * 1024 ** 2
    assert cc.requests_needed(good(worker_replicas=0), None)[0] == 2000


def test_a_display_name_with_underscores_is_a_valid_image():
    assert "image" not in [k for k, _ in cc.validate(
        good(image="default/sles15-sp7-minimal-vm.x86_64-cloud-qu2.qcow2"))]


def test_the_real_v1beta2_cluster_reads_ready():
    """Le cœur de Turtles répond en v1beta2 : `status.controlPlaneReady`
    n'existe plus, l'ancienne attente finissait toujours sur délai dépassé."""
    st = cc.cluster_state(load("cluster-v1beta2-available.json"),
                          load("machines-running.json")["items"])
    assert st["infrastructure"] and st["control_plane"] and st["ready"]
    assert st["cp"] == [1, 1] and st["workers"] == [2, 2]
    assert cc.class_of(load("cluster-v1beta2-available.json")) == "harvester-rke2"


def test_a_v1beta1_cluster_still_reads():
    old = {"status": {"phase": "Provisioned", "controlPlaneReady": True,
                      "infrastructureReady": True},
           "spec": {"topology": {"class": "harvester-rke2"}}}
    machines = [{"metadata": {"labels": {"cluster.x-k8s.io/control-plane": ""}},
                 "status": {"phase": "Running", "nodeRef": {"name": "n1"}}}]
    st = cc.cluster_state(old, machines)
    assert st["control_plane"] and st["infrastructure"] and st["ready"]
    assert cc.class_of(old) == "harvester-rke2"


def test_the_expected_counts_come_from_the_topology():
    """Vu dans la page : « plan de contrôle 0/0, workers 0/0 » tant qu'aucune
    machine n'existait ; le nombre voulu est dans la topologie."""
    cl = {"spec": {"topology": {"controlPlane": {"replicas": 3},
                                "workers": {"machineDeployments": [{"replicas": 2},
                                                                   {"replicas": 1}]}}},
          "status": {"phase": "Provisioning"}}
    st = cc.cluster_state(cl, [])
    assert st["cp"] == [0, 3] and st["workers"] == [0, 3] and not st["ready"]


def test_available_before_the_workers_exist_is_not_ready():
    """Vu sur harv1 (essai2) : le cluster se disait Available avec le plan de
    contrôle seul, la MachineDeployment n'étant créée qu'ensuite."""
    cl = load("cluster-v1beta2-available.json")
    cl["spec"]["topology"]["workers"] = {"machineDeployments": [{"class": "default-worker",
                                                                 "name": "workers", "replicas": 1}]}
    cp_only = [m for m in load("machines-running.json")["items"]
               if "cluster.x-k8s.io/control-plane" in m["metadata"]["labels"]]
    st = cc.cluster_state(cl, cp_only)
    assert st["cp"] == [1, 1] and st["workers"] == [0, 1] and not st["ready"]


def test_a_registered_but_not_ready_node_does_not_count():
    """Vu sur harv1 (essai3) : « disponible » annoncé à 18:58:02, le nœud du
    worker Ready à 18:58:30, la condition Available à 18:58:31."""
    cl = load("cluster-v1beta2-available.json")
    for c in cl["status"]["conditions"]:
        if c["type"] == "Available":
            c["status"] = "False"
    machines = load("machines-running.json")["items"]
    worker = [m for m in machines if "cluster.x-k8s.io/control-plane" not in m["metadata"]["labels"]][0]
    for c in worker["status"]["conditions"]:
        if c["type"] in ("NodeReady", "NodeHealthy", "Ready", "Available"):
            c["status"] = "False"
    st = cc.cluster_state(cl, machines)
    assert st["workers"][0] == st["workers"][1] - 1
    assert not st["ready"]                   # Available False : pas de repli


def test_the_list_view_reads_the_counters_of_the_status():
    """La liste des clusters n'a pas les machines : vu sur harv1, essai3
    disponible s'y affichait « … » (0 machine prête sur 1 voulue)."""
    cl = load("cluster-v1beta2-available.json")
    st = cc.cluster_state(cl)
    assert st["ready"] and st["cp"] == [1, 1] and st["workers"] == [2, 2]
    cl["status"]["workers"]["availableReplicas"] = 1
    for c in cl["status"]["conditions"]:
        if c["type"] == "Available":
            c["status"] = "False"
    assert not cc.cluster_state(cl)["ready"]


def test_a_cluster_on_its_way_is_not_ready_and_says_why():
    cl = load("cluster-v1beta2-available.json")
    for c in cl["status"]["conditions"]:
        if c["type"] == "Available":
            c["status"], c["message"] = "False", "* ControlPlaneAvailable: waiting"
    st = cc.cluster_state(cl, [])
    assert not st["ready"] and "ControlPlaneAvailable" in st["message"]


# ---------------------------------------------------------------------------
# v1.52.0 : kube-vip dans chaque cluster créé.
#
# Vu sur harv1 le 26/09/2026 : le fournisseur de cloud Harvester posé par
# CAPHV demande à kube-vip d'annoncer l'adresse des services LoadBalancer
# (`kube-vip.io/loadbalancerIPs`), mais CAPHV ne pose pas kube-vip : podinfo
# est resté sans adresse et sa release a échoué au bout de dix minutes.
# ---------------------------------------------------------------------------
def test_created_clusters_carry_kube_vip():
    docs = [{"apiVersion": "cluster.x-k8s.io/v1beta1", "kind": "Cluster",
             "metadata": {"name": "web", "namespace": "web", "labels": {"ccm": "external"}},
             "spec": {"topology": {"variables": []}}}]
    out = cc.postprocess(docs, {})
    cm = next(d for d in out if d["kind"] == "ConfigMap" and d["metadata"]["name"] == cc.KUBE_VIP_CM)
    crs = next(d for d in out if d["kind"] == "ClusterResourceSet")
    assert cm["metadata"]["namespace"] == crs["metadata"]["namespace"] == "web"
    assert cm["metadata"]["labels"][cc.GENERATED] == crs["metadata"]["labels"][cc.GENERATED] == "true"
    assert crs["spec"]["clusterSelector"] == {"matchLabels": {"ccm": "external"}}
    assert crs["spec"]["resources"] == [{"kind": "ConfigMap", "name": cc.KUBE_VIP_CM}]
    ds = [d for d in yaml.safe_load_all(cm["data"]["kube-vip.yaml"]) if d and d["kind"] == "DaemonSet"][0]
    env = {e["name"]: e["value"] for e in ds["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["svc_enable"] == "true" and env["cp_enable"] == "false"
    assert ds["spec"]["template"]["spec"]["hostNetwork"] is True
    # une seule fois, même repassé
    again = cc.postprocess(out, {})
    assert sum(1 for d in again if d["kind"] == "ClusterResourceSet") == 1
