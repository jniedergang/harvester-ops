"""2.10.1 tâche 4 (point 322 A) : `hops-allow-ingress` admet les deux
contrôleurs d'ingress de RKE2.

RKE2 v1.36 déploie Traefik par défaut sur un cluster neuf (docs RKE2,
networking_services) et la console propose v1.36.4 : la politique d'origine
n'admettait que les pods `rke2-ingress-nginx` et laissait toute Ingress
d'équipe en délai dépassé. Les politiques déjà posées sur un enfant ne sont
pas réécrites en silence : la console les dit périmées
(`child-ingress-policy-stale`), elles se repassent par le chemin de la 2.7.0.

Hypothèse à confirmer au banc (tâche 1, essai 7, enfant v1.36.4) : les pods
du chart rke2-traefik portent `app.kubernetes.io/name: traefik` (étiquette de
sélection du chart amont traefik, `traefik.labelselector`), dans kube-system.

Mutations rapportées (rapport 2.10.1, tâche 4) : « étiquette Traefik
retirée » -> `test_ingress_admits_both_controllers[traefik]` échoue ;
« péremption jamais dite » -> `test_stale_ingress` échoue.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import child_policies as cp  # noqa: E402
import rke2_offline as ro  # noqa: E402

KS = {"kubernetes.io/metadata.name": "kube-system"}
NGINX = {"app.kubernetes.io/name": "rke2-ingress-nginx", "app.kubernetes.io/component": "controller",
         "app.kubernetes.io/instance": "rke2-ingress-nginx"}
TRAEFIK = {"app.kubernetes.io/name": "traefik", "app.kubernetes.io/instance": "rke2-traefik-kube-system"}


def _matches(peer, ns_labels, pod_labels):
    """Sémantique NetworkPolicy d'un pair `namespaceSelector` + `podSelector`
    (matchLabels seulement, ce que rend la console)."""
    nsel = (peer.get("namespaceSelector") or {}).get("matchLabels") or {}
    psel = (peer.get("podSelector") or {}).get("matchLabels") or {}
    return all(ns_labels.get(k) == v for k, v in nsel.items()) and \
        all(pod_labels.get(k) == v for k, v in psel.items())


def _allow_ingress(ns="equipe-a"):
    spec = cp.normalize({"base": True, "teams": [{"namespace": ns}]})
    return next(o for o in cp.render(spec)["base"] if o["metadata"]["name"] == "hops-allow-ingress")


def test_selectors_exact():
    assert cp.ingress_selectors() == [
        {"namespaceSelector": {"matchLabels": KS},
         "podSelector": {"matchLabels": {"app.kubernetes.io/name": "rke2-ingress-nginx",
                                         "app.kubernetes.io/component": "controller"}}},
        {"namespaceSelector": {"matchLabels": KS},
         "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}}}]
    assert _allow_ingress()["spec"]["ingress"] == [{"from": cp.ingress_selectors()}]


@pytest.mark.parametrize("label,ns,pod,admitted", [
    ("nginx", KS, NGINX, True),
    ("traefik", KS, TRAEFIK, True),
    ("nginx outside kube-system", {"kubernetes.io/metadata.name": "equipe-b"}, NGINX, False),
    ("traefik outside kube-system", {"kubernetes.io/metadata.name": "equipe-b"}, TRAEFIK, False),
    ("another kube-system pod", KS, {"k8s-app": "kube-dns"}, False),
    ("nginx admission webhook", KS, dict(NGINX, **{"app.kubernetes.io/component": "admission-webhook"}), False),
])
def test_ingress_admits_both_controllers(label, ns, pod, admitted):
    peers = _allow_ingress()["spec"]["ingress"][0]["from"]
    assert any(_matches(p, ns, pod) for p in peers) is admitted, label


def _old_policy(ns):
    """`hops-allow-ingress` posée par la 2.7.0 à 2.10.0 (ingress-nginx seul)."""
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "hops-allow-ingress", "namespace": ns},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"],
                     "ingress": [{"from": [cp.ingress_selectors()[0]]}]}}


@pytest.mark.parametrize("label,objs,want", [
    ("current policies", lambda: cp.render(cp.normalize({"base": True, "teams": [{"namespace": "a"}]}))["base"], []),
    ("policies of 2.10.0", lambda: [_old_policy("b"), _old_policy("a")], ["a", "b"]),
    ("one stale, one current", lambda: [_old_policy("b"), _allow_ingress("a")], ["b"]),
    ("no ingress policy (allow_ingress off)", lambda: cp.render(cp.normalize(
        {"base": True, "teams": [{"namespace": "a", "network": {"allow_ingress": False}}]}))["base"], []),
    ("nothing", lambda: [], []),
])
def test_stale_ingress(label, objs, want):
    assert cp.stale_ingress(objs()) == want, label


def test_offline_list_keeps_both_ingress_controllers():
    text = "\n".join(["docker.io/rancher/rke2-runtime:v1.36.4-rke2r1",
                      "docker.io/rancher/mirrored-library-traefik:3.5.1",
                      "docker.io/rancher/nginx-ingress-controller:v1.13.1-hardened1",
                      "docker.io/rancher/hardened-calico:v3.30.2-build20250820",
                      "docker.io/rancher/hardened-flannel:v0.27.2-build20250820"])
    got = ro.images_for(text, "calico")
    assert "docker.io/rancher/mirrored-library-traefik:3.5.1" in got
    assert "docker.io/rancher/nginx-ingress-controller:v1.13.1-hardened1" in got


@pytest.mark.parametrize("label,objs,want", [
    ("policies of 2.10.0", lambda: [_old_policy("equipe-a")], ["equipe-a"]),
    ("current policies", lambda: cp.objects(cp.normalize({"teams": [{"namespace": "equipe-a"}]})), []),
])
def test_manifests_say_the_stale_ingress_policy(monkeypatch, capsys, label, objs, want):
    import importlib.util
    import json
    import capi_manifests as mf
    spec = importlib.util.spec_from_file_location("harvester_capi_2101_t4", ROOT / "bin" / "harvester-capi.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    cm, crs = mf.docs("web", "web", cp.ITEM, "harvester-ops", "builtin", objs(), by="alice")

    class K:
        def list(self, kind, ns=None, selector=None):
            return [crs] if kind == mf.K_CRS else []

        def get(self, kind, ns, name):
            return cm if name == cm["metadata"]["name"] else None
    monkeypatch.setattr(cli, "kube_from", lambda a: (K(), None, None))
    assert cli.main(["manifests", "--json"]) == cli.EXIT_OK
    row = json.loads(capsys.readouterr().out)[0]
    assert row["stale_ingress"] == want, label
