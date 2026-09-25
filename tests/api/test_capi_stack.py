"""v1.48.0 : la pile Cluster API d'un Harvester 1.9, par Rancher Turtles.

Ce que ces tests tiennent, tel que vu sur harv1 le 25/09/2026 :

- Harvester 1.9 embarque Turtles et son cœur CAPI ; la console déclare les
  fournisseurs manquants en `CAPIProvider`, composants tirés de ConfigMaps
  (rien d'Internet), et ne réinstalle jamais de cœur ni de cert-manager ;
- CAPHV <= v0.10.1 cherche l'adresse de Harvester dans un service que
  Harvester 1.9 a retiré : la console le recrée, étiqueté, seulement s'il
  manque (un Harvester 1.8 l'a d'origine) ;
- une installation d'avant Turtles se retire sans toucher à ce que Turtles a
  repris, et jamais tant qu'un cluster existe.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import capi_stack as cs  # noqa: E402
from kube import KubeError  # noqa: E402

KEYS = {"CAPIProvider": cs.K_CAPIPROVIDER, "ConfigMap": "configmaps", "Namespace": "namespaces",
        "Service": "services"}


class FakeKube:
    """kubectl en mémoire. `turtles=False` : la CRD CAPIProvider n'existe pas.
    Un CAPIProvider déclaré devient prêt, son déploiement disponible."""

    def __init__(self, turtles=True, traefik_ip="172.16.3.100", ingress_expose=None):
        self.objs = {}
        self.turtles = turtles
        self.applied, self.deleted = [], []
        self.put("settings.harvesterhci.io", None, "server-version", {"value": "v1.9.0"})
        if turtles:
            self.put(cs.K_CAPIPROVIDER, "cattle-capi-system", "cluster-api", {
                "spec": {"name": "cluster-api", "type": "core", "version": "v1.13.3"},
                "status": {"phase": "Ready", "installedVersion": "v1.13.3",
                           "conditions": [{"type": "Ready", "status": "True"}]}})
        if traefik_ip:
            self.put("services", "kube-system", "rke2-traefik",
                     {"status": {"loadBalancer": {"ingress": [{"ip": traefik_ip}]}}})
        if ingress_expose is not None:
            self.put("services", "kube-system", "ingress-expose", ingress_expose)
        for ns in ("default", "kube-system", "cattle-capi-system"):
            self.put("namespaces", None, ns, {})

    def put(self, kind, ns, name, body):
        body = json.loads(json.dumps(body))
        md = body.setdefault("metadata", {})
        md["name"] = name
        if ns:
            md["namespace"] = ns
        self.objs[(kind, ns, name)] = body

    def get(self, kind, ns, name):
        if kind == cs.K_CAPIPROVIDER and not self.turtles:
            raise KubeError("the server doesn't have a resource type \"capiproviders\"")
        o = self.objs.get((kind, ns, name))
        return json.loads(json.dumps(o)) if o is not None else None

    def list(self, kind, ns=None, selector=None):
        if kind == cs.K_CAPIPROVIDER and not self.turtles:
            raise KubeError("the server doesn't have a resource type \"capiproviders\"")
        return [json.loads(json.dumps(o)) for (k, n, _), o in self.objs.items()
                if k == kind and (ns is None or n == ns)]

    def apply(self, docs, **kw):
        for d in docs:
            kind = KEYS[d["kind"]]
            ns = d["metadata"].get("namespace")
            self.applied.append((d["kind"], ns, d["metadata"]["name"]))
            self.put(kind, ns, d["metadata"]["name"], d)
            if d["kind"] == "CAPIProvider":
                o = self.objs[(kind, ns, d["metadata"]["name"])]
                o["status"] = {"phase": "Ready", "installedVersion": d["spec"]["version"],
                               "conditions": [{"type": "Ready", "status": "True"}]}
                self.put("deployments", ns, "manager", {"spec": {"replicas": 1},
                                                        "status": {"availableReplicas": 1}})
        return ""

    def patch(self, kind, ns, name, patch):
        o = self.objs[(kind, ns, name)]
        for k, v in ((patch.get("metadata") or {}).get("labels") or {}).items():
            labels = o["metadata"].setdefault("labels", {})
            if v is None:
                labels.pop(k, None)
            else:
                labels[k] = v

    def delete(self, kind, ns, name, cascade=None):
        self.deleted.append((kind, ns, name))
        self.objs.pop((kind, ns, name), None)


def desc(tmp_path, big=False):
    """Un paquet extrait minimal, dans le format de bundle-capi.sh."""
    providers = []
    for key, name, ptype, ns, ver, extra in (
            ("rke2-bootstrap", "rke2", "bootstrap", "rke2-bootstrap-system", "v0.25.2", {}),
            ("rke2-control-plane", "rke2", "controlPlane", "rke2-control-plane-system", "v0.25.2", {}),
            ("harvester", "harvester", "infrastructure", "caphv-system", "v0.10.1",
             {"workarounds": ["ingress-expose"],
              "patches": [{"target": {"kind": "CustomResourceDefinition",
                                      "name": "harvestermachines.infrastructure.cluster.x-k8s.io"},
                           "patch": "- op: remove\n  path: /metadata/labels/cluster.x-k8s.io~1v1beta2\n"}]})):
        d = tmp_path / "turtles" / key
        d.mkdir(parents=True)
        (d / "components.yaml").write_text("kind: Deployment\n" + ("x" * 1_000_000 if big and key == "harvester" else ""))
        (d / "metadata.yaml").write_text("releaseSeries: []\n")
        providers.append(dict({"key": key, "name": name, "type": ptype, "namespace": ns,
                               "version": ver}, **extra))
    (tmp_path / "turtles.json").write_text(json.dumps({"providers": providers,
                                                       "kubernetes_versions": ["v1.34.11"]}))
    return cs.load_components(tmp_path)


def steps():
    seen = []
    return seen, (lambda sid, st, msg="": seen.append((sid, st, msg)))


# -- état ----------------------------------------------------------------------

def test_a_fresh_harvester_19_lacks_the_providers_and_the_shim(tmp_path):
    d = desc(tmp_path)
    st = cs.stack_status(FakeKube(), d["providers"])
    assert st["turtles"] and st["core"]["version"] == "v1.13.3" and st["core"]["ready"]
    assert st["missing"] == ["bootstrap/rke2", "controlPlane/rke2", "infrastructure/harvester",
                             "compatibility/ingress-expose"]
    assert not st["ready"] and not st["legacy"] and st["harvester_version"] == "v1.9.0"


def test_without_turtles_nothing_can_be_installed(tmp_path):
    d = desc(tmp_path)
    kube = FakeKube(turtles=False)
    st = cs.stack_status(kube, d["providers"])
    assert st["missing"][0] == "turtles" and not st["ready"]
    with pytest.raises(RuntimeError, match="Turtles"):
        cs.install(kube, d, steps()[1])


def test_a_harvester_18_keeps_its_own_ingress_expose(tmp_path):
    d = desc(tmp_path)
    native = {"metadata": {"annotations": {"kube-vip.io/loadbalancerIPs": "10.0.0.5"}}}
    st = cs.stack_status(FakeKube(ingress_expose=native), d["providers"])
    assert st["shim"] == {"needed": False, "present": True, "ours": False, "vip": None}
    assert "compatibility/ingress-expose" not in st["missing"]


# -- installation --------------------------------------------------------------

def test_install_declares_the_providers_from_configmaps(tmp_path):
    d = desc(tmp_path)
    kube = FakeKube()
    seen, step = steps()
    st = cs.install(kube, d, step, sleep=lambda s: None)
    assert st["ready"] and st["missing"] == []
    cm = kube.get("configmaps", "caphv-system", "v0.10.1")
    assert cm["metadata"]["labels"]["provider.cluster.x-k8s.io/name"] == "harvester"
    assert cm["metadata"]["labels"]["provider.cluster.x-k8s.io/type"] == "infrastructure"
    assert cm["data"]["components"].startswith("kind: Deployment")
    prov = kube.get(cs.K_CAPIPROVIDER, "caphv-system", "harvester")
    assert prov["spec"]["fetchConfig"]["selector"]["matchLabels"] == {
        "provider.cluster.x-k8s.io/name": "harvester",
        "provider.cluster.x-k8s.io/type": "infrastructure"}
    # le contournement du contrat v1beta2, porté par le CAPIProvider lui-même
    # (Turtles remet le label si on l'ôte à la main : vu sur harv1)
    assert prov["spec"]["patches"][0]["target"]["name"].startswith("harvestermachines.")
    shim = kube.get("services", "kube-system", "ingress-expose")
    assert shim["metadata"]["annotations"]["kube-vip.io/loadbalancerIPs"] == "172.16.3.100"
    assert shim["metadata"]["labels"][cs.SHIM_LABEL] == "caphv-ingress-expose"
    assert ("compatibility", "done", "ingress-expose recreated for CAPHV (VIP 172.16.3.100)") in seen
    # jamais de cœur ni de cert-manager
    assert not [a for a in kube.applied if a[2] in ("cluster-api", "cert-manager")]


CRD_HM = "harvestermachines.infrastructure.cluster.x-k8s.io"


def test_install_removes_a_contract_label_left_on_the_crd(tmp_path):
    """Vu sur harv1 : fournisseur déclaré un temps sans le correctif (la CRD
    reprend l'étiquette), puis réinstallé avec ; Turtles applique les
    composants corrigés « depuis son cache » sans retirer l'étiquette déjà
    posée, et le statut se disait prêt."""
    d = desc(tmp_path)
    kube = FakeKube()
    kube.put(cs.K_CRD, None, CRD_HM, {"metadata": {"labels": {
        "cluster.x-k8s.io/v1beta1": "v1alpha1", "cluster.x-k8s.io/v1beta2": "v1alpha1"}}})
    before = cs.stack_status(kube, d["providers"])
    assert "compatibility/contract-label" in before["missing"]
    seen, step = steps()
    st = cs.install(kube, d, step, sleep=lambda s: None)
    labels = kube.get(cs.K_CRD, None, CRD_HM)["metadata"]["labels"]
    assert "cluster.x-k8s.io/v1beta2" not in labels and labels["cluster.x-k8s.io/v1beta1"] == "v1alpha1"
    assert st["ready"] and st["contract"] == []
    assert ("compatibility", "done", f"label cluster.x-k8s.io/v1beta2 removed from {CRD_HM}") in seen


def test_install_is_idempotent(tmp_path):
    d = desc(tmp_path)
    kube = FakeKube()
    cs.install(kube, d, steps()[1], sleep=lambda s: None)
    n = len([o for o in kube.objs if o[0] == "services"])
    st = cs.install(kube, d, steps()[1], sleep=lambda s: None)
    assert st["ready"] and len([o for o in kube.objs if o[0] == "services"]) == n


def test_big_components_are_compressed(tmp_path):
    cm = cs.configmap_manifest(desc(tmp_path, big=True)["providers"][2])
    assert cm["metadata"]["annotations"]["provider.cluster.x-k8s.io/compressed"] == "true"
    assert "components" in cm["binaryData"] and "components" not in cm["data"]


def test_install_gives_up_on_a_provider_that_never_gets_ready(tmp_path):
    d = desc(tmp_path)
    kube = FakeKube()
    real_apply = kube.apply

    def apply(docs, **kw):
        real_apply(docs, **kw)
        for doc in docs:
            if doc["kind"] == "CAPIProvider" and doc["metadata"]["name"] == "harvester":
                kube.objs[(cs.K_CAPIPROVIDER, "caphv-system", "harvester")]["status"]["phase"] = "Provisioning"
    kube.apply = apply
    clock = iter(range(0, 10_000, 100))
    with pytest.raises(RuntimeError, match="harvester"):
        cs.install(kube, d, steps()[1], timeout=300, now=lambda: next(clock), sleep=lambda s: None)


def test_no_vip_no_shim(tmp_path):
    d = desc(tmp_path)
    with pytest.raises(RuntimeError, match="rke2-traefik"):
        cs.install(FakeKube(traefik_ip=None), d, steps()[1], sleep=lambda s: None)


# -- retrait d'une ancienne installation --------------------------------------

def legacy_kube():
    kube = FakeKube()
    for ns in cs.LEGACY_NAMESPACES:
        kube.put("namespaces", None, ns, {})
    kube.put(cs.K_CLUSTERCLASS, "default", "harvester-rke2", {})
    kube.put(cs.K_CLUSTER, "fleet-local", "local", {})
    kube.put("validatingwebhookconfigurations", None, "capi-kubeadm-bootstrap-validating", {
        "webhooks": [{"clientConfig": {"service": {"namespace": "capi-kubeadm-bootstrap-system"}}}]})
    kube.put("validatingwebhookconfigurations", None, "capi-validating", {
        "webhooks": [{"clientConfig": {"service": {"namespace": "cattle-capi-system"}}}]})
    kube.put(cs.K_CRD, None, "rke2configs.bootstrap.cluster.x-k8s.io", {"spec": {"conversion": {
        "webhook": {"clientConfig": {"service": {"namespace": "rke2-bootstrap-system"}}}}}})
    # une CRD reprise par Turtles : intouchable
    kube.put(cs.K_CRD, None, "kubeadmconfigs.bootstrap.cluster.x-k8s.io", {
        "metadata": {"annotations": {"objectset.rio.cattle.io/applied": "x"}}, "spec": {}})
    kube.put(cs.K_CRD, None, "clusters.cluster.x-k8s.io", {"spec": {}})
    kube.put("clusterrolebindings", None, "caphv-manager-rolebinding", {
        "roleRef": {"name": "caphv-manager-role"},
        "subjects": [{"kind": "ServiceAccount", "namespace": "caphv-system", "name": "m"}]})
    kube.put("clusterrolebindings", None, "shared-binding", {
        "roleRef": {"name": "caphv-manager-role"},
        "subjects": [{"kind": "ServiceAccount", "namespace": "cattle-capi-system", "name": "m"}]})
    kube.put("clusterrolebindings", None, "rke2-bootstrap-manager-rolebinding", {
        "roleRef": {"name": "rke2-bootstrap-manager-role"},
        "subjects": [{"kind": "ServiceAccount", "namespace": "rke2-bootstrap-system", "name": "m"}]})
    return kube


def test_the_legacy_plan_spares_what_turtles_owns():
    plan = cs.legacy_plan(legacy_kube())
    names = [p[2] for p in plan]
    assert "harvester-rke2" in names and "capi-kubeadm-bootstrap-validating" in names
    assert "rke2configs.bootstrap.cluster.x-k8s.io" in names
    assert set(cs.LEGACY_NAMESPACES) <= set(names)
    assert "rke2-bootstrap-manager-rolebinding" in names and "rke2-bootstrap-manager-role" in names
    # repris par Turtles, ou partagé : jamais
    for kept in ("kubeadmconfigs.bootstrap.cluster.x-k8s.io", "clusters.cluster.x-k8s.io",
                 "capi-validating", "shared-binding", "caphv-manager-role"):
        assert kept not in names
    assert "caphv-manager-rolebinding" in names
    # la ClusterClass passe avant les CRD, les espaces avant les rôles
    assert names.index("harvester-rke2") < names.index("rke2configs.bootstrap.cluster.x-k8s.io")


def test_no_legacy_cleanup_while_a_cluster_exists():
    kube = legacy_kube()
    kube.put(cs.K_CLUSTER, "web", "web", {})
    with pytest.raises(RuntimeError, match="web/web"):
        cs.legacy_plan(kube)


def test_a_namespace_owned_by_a_capiprovider_is_not_legacy(tmp_path):
    kube = FakeKube()
    cs.install(kube, desc(tmp_path), steps()[1], sleep=lambda s: None)
    assert cs.legacy_plan(kube) == []
    assert cs.stack_status(kube)["legacy"] is False


def test_cleanup_dry_run_deletes_nothing():
    kube = legacy_kube()
    seen, step = steps()
    plan = cs.cleanup_legacy(kube, step, dry_run=True)
    assert plan and kube.deleted == []
    assert seen[-1][1] == "done" and "to remove" in seen[-1][2]
    cs.cleanup_legacy(kube, step)
    assert len(kube.deleted) == len(plan)
