"""v1.52.0 (B2) : les sous-commandes `harvester-capi service-*`.

Un faux cluster de gestion tient les objets : on vérifie que le déploiement
déclare le service, étiquette le cluster visé et suit la release jusqu'à
« prête » ; qu'il ne touche à rien quand CAAPH manque ; que le retrait
supprime le service et ôte l'étiquette.
"""

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("harvester_capi_svc", ROOT / "bin" / "harvester-capi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Kube:
    """Le strict nécessaire : list, get, apply, patch, delete."""

    def __init__(self, objs, on_apply=None):
        self.objs = objs                    # {(kind, ns, name): objet}
        self.applied, self.patched, self.deleted = [], [], []
        self.on_apply = on_apply

    def list(self, kind, ns=None, selector=None):
        return [o for (k, s, _), o in self.objs.items() if k == kind and (ns is None or s == ns)]

    def get(self, kind, ns, name):
        return self.objs.get((kind, ns, name))

    RESOURCE = {"ConfigMap": "configmaps",
                "ClusterResourceSet": "clusterresourcesets.addons.cluster.x-k8s.io",
                "HelmChartProxy": "helmchartproxies.addons.cluster.x-k8s.io"}

    def apply(self, docs):
        self.applied.extend(docs)
        for d in docs:
            md = d["metadata"]
            self.objs[(self.RESOURCE.get(d["kind"], d["kind"]), md.get("namespace"), md["name"])] = d
        if self.on_apply:
            self.on_apply(self)

    def patch(self, kind, ns, name, body):
        self.patched.append((kind, ns, name, body))
        labels = self.objs[(kind, ns, name)]["metadata"].setdefault("labels", {})
        for k, v in body["metadata"]["labels"].items():
            if v is None:
                labels.pop(k, None)
            else:
                labels[k] = v

    def delete(self, kind, ns, name, cascade=None):
        self.deleted.append((kind, ns, name))
        self.objs.pop((kind, ns, name), None)


def cluster_obj(ns, name, labels=None):
    return {"metadata": {"namespace": ns, "name": name, "labels": dict(labels or {})}}


def release(ns, owner, cluster, ready=True, status="deployed"):
    return {"metadata": {"namespace": ns, "name": f"{owner}-{cluster}-x",
                         "ownerReferences": [{"kind": "HelmChartProxy", "name": owner}]},
            "spec": {"clusterRef": {"name": cluster}},
            "status": {"status": status, "revision": 1,
                       "conditions": [{"type": "Ready", "status": "True" if ready else "False",
                                       "message": "" if ready else "installing"}]}}


def spec_file(tmp_path, **over):
    body = {"service": "podinfo", "cluster": "essai/essai"}
    body.update(over)
    p = tmp_path / "svc.json"
    p.write_text(json.dumps(body))
    return str(p)


def events(capsys):
    return [ln.split("|", 3)[2:] for ln in capsys.readouterr().err.splitlines()
            if ln.startswith("STEP_EVENT|")]


def setup(cli, monkeypatch, kube, caaph=True):
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    monkeypatch.setattr(cli, "_caaph_ready", lambda k: caaph)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)


def test_deploy_declares_labels_and_follows_until_ready(cli, monkeypatch, tmp_path, capsys):
    sv, cs = cli.sv, cli.cs
    kube = Kube({(cs.K_CLUSTER, "essai", "essai"): cluster_obj("essai", "essai")},
                on_apply=lambda k: k.objs.__setitem__((sv.K_HRP, "essai", "r"), release("essai", "podinfo", "essai")))
    setup(cli, monkeypatch, kube)
    rc = cli.cmd_service_deploy(argparse.Namespace(spec=spec_file(tmp_path), timeout=60))
    assert rc == cli.EXIT_OK
    hcp = kube.applied[0]
    assert hcp["kind"] == "HelmChartProxy" and hcp["metadata"]["namespace"] == "essai"
    assert hcp["spec"]["clusterSelector"]["matchLabels"] == {"harvester-ops.io/svc-podinfo": "on"}
    assert kube.objs[(cs.K_CLUSTER, "essai", "essai")]["metadata"]["labels"] == {"harvester-ops.io/svc-podinfo": "on"}
    ev = events(capsys)
    assert ev[-1][0] == "done" and "podinfo installed on essai" in ev[-1][1]


def test_nothing_is_touched_when_caaph_is_missing(cli, monkeypatch, tmp_path, capsys):
    cs = cli.cs
    kube = Kube({(cs.K_CLUSTER, "essai", "essai"): cluster_obj("essai", "essai")})
    setup(cli, monkeypatch, kube, caaph=False)
    rc = cli.cmd_service_deploy(argparse.Namespace(spec=spec_file(tmp_path), timeout=60))
    assert rc == cli.EXIT_BLOCKED
    assert kube.applied == [] and kube.patched == []
    assert any(e[0] == "error" and e[1].startswith("caaph-missing ") for e in events(capsys))


def test_a_release_that_never_gets_ready_fails_with_its_message(cli, monkeypatch, tmp_path, capsys):
    sv, cs = cli.sv, cli.cs
    kube = Kube({(cs.K_CLUSTER, "essai", "essai"): cluster_obj("essai", "essai")},
                on_apply=lambda k: k.objs.__setitem__((sv.K_HRP, "essai", "r"),
                                                      release("essai", "podinfo", "essai", ready=False, status="pending-install")))
    setup(cli, monkeypatch, kube)
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(cli.time, "time", lambda: next(clock))
    rc = cli.cmd_service_deploy(argparse.Namespace(spec=spec_file(tmp_path), timeout=60))
    assert rc == cli.EXIT_FAIL
    last = events(capsys)[-1]
    assert last[0] == "error" and "pending-install" in last[1] and "installing" in last[1]


def test_remove_deletes_the_service_and_unlabels_the_cluster(cli, monkeypatch, capsys):
    sv, cs = cli.sv, cli.cs
    label = sv.label_of("podinfo")
    kube = Kube({(cs.K_CLUSTER, "essai", "essai"): cluster_obj("essai", "essai", {label: "on", "keep": "me"}),
                 (sv.K_HCP, "essai", "podinfo"): {"metadata": {"namespace": "essai", "name": "podinfo"}}})
    setup(cli, monkeypatch, kube)
    rc = cli.cmd_service_remove(argparse.Namespace(name="essai/podinfo", timeout=60))
    assert rc == cli.EXIT_OK
    assert (sv.K_HCP, "essai", "podinfo") in kube.deleted
    assert kube.objs[(cs.K_CLUSTER, "essai", "essai")]["metadata"]["labels"] == {"keep": "me"}


def test_removing_an_unknown_service_says_so(cli, monkeypatch, capsys):
    setup(cli, monkeypatch, Kube({}))
    assert cli.cmd_service_remove(argparse.Namespace(name="essai/nope", timeout=60)) == cli.EXIT_FAIL
    assert "not found" in events(capsys)[-1][1]


def test_the_listing_leaves_the_rancher_namespaces_out(cli, monkeypatch, capsys):
    cs = cli.cs
    kube = Kube({(cs.K_CLUSTER, "essai", "essai"): cluster_obj("essai", "essai"),
                 (cs.K_CLUSTER, "fleet-local", "local"): cluster_obj("fleet-local", "local")})
    setup(cli, monkeypatch, kube)
    assert cli.cmd_services(argparse.Namespace(json=True)) == cli.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["clusters"] == ["essai/essai"] and out["caaph"] is True
    assert [c["key"] for c in out["catalog"]] == ["coredns", "podinfo"]


def test_an_older_cluster_gets_kube_vip_before_its_first_service(cli, monkeypatch, tmp_path, capsys):
    sv, cs, cc = cli.sv, cli.cs, cli.cc
    kube = Kube({(cs.K_CLUSTER, "essai", "essai"): cluster_obj("essai", "essai", {"ccm": "external"})},
                on_apply=lambda k: k.objs.__setitem__((sv.K_HRP, "essai", "r"), release("essai", "podinfo", "essai")))
    setup(cli, monkeypatch, kube)
    assert cli.cmd_service_deploy(argparse.Namespace(spec=spec_file(tmp_path), timeout=60)) == cli.EXIT_OK
    kinds = [d["kind"] for d in kube.applied]
    assert kinds[:2] == ["ConfigMap", "ClusterResourceSet"] and kinds[2] == "HelmChartProxy"
    assert any(e[0] == "done" and "kube-vip added" in e[1] for e in events(capsys))
    # déjà là : pas reposé
    kube.applied.clear()
    assert cli.cmd_service_deploy(argparse.Namespace(spec=spec_file(tmp_path), timeout=60)) == cli.EXIT_OK
    assert [d["kind"] for d in kube.applied] == ["HelmChartProxy"]
    assert cli._clusters_without_kube_vip(kube) == []


def test_an_update_waits_for_caaph_to_take_the_new_version(cli, monkeypatch, tmp_path, capsys):
    """Vu sur harv1 le 26/09/2026 : la mise à jour d'un service finissait en
    6 s sur l'ancienne release, encore prête, avant que CAAPH n'ait lu les
    nouvelles valeurs."""
    sv, cs = cli.sv, cli.cs
    hcp_key, hrp_key = (sv.K_HCP, "essai", "podinfo"), (sv.K_HRP, "essai", "r")
    old = release("essai", "podinfo", "essai")
    old["metadata"]["generation"], old["status"]["observedGeneration"] = 1, 1
    kube = Kube({(cs.K_CLUSTER, "essai", "essai"): cluster_obj("essai", "essai"),
                 hcp_key: {"metadata": {"namespace": "essai", "name": "podinfo", "generation": 1},
                           "status": {"observedGeneration": 1}},
                 hrp_key: old})

    def bump(k):                                   # l'API : la spec change, la génération monte
        k.objs[hcp_key]["metadata"]["generation"] = 2
        k.objs[hcp_key]["status"] = {"observedGeneration": 1}
    kube.on_apply = bump
    ticks = []

    def controller(_s):                            # CAAPH, un pas par attente
        ticks.append(1)
        if len(ticks) == 1:                        # le proxy relu, la release à refaire
            kube.objs[hcp_key]["status"]["observedGeneration"] = 2
            kube.objs[hrp_key]["metadata"]["generation"] = 2
        elif len(ticks) == 2:                      # helm upgrade fait
            kube.objs[hrp_key]["status"].update({"observedGeneration": 2, "revision": 2})
    setup(cli, monkeypatch, kube)
    monkeypatch.setattr(cli.time, "sleep", controller)
    assert cli.cmd_service_deploy(argparse.Namespace(spec=spec_file(tmp_path), timeout=600)) == cli.EXIT_OK
    assert len(ticks) >= 2
    assert "revision 2" in events(capsys)[-1][1]
