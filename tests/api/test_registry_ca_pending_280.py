"""Revue des tâches 5 à 8 (I-6) : `harvester-registry` ne bascule le
certificat sur la nouvelle CA que quand CHAQUE copie de la CA la porte :
Secret des nœuds désigné par les gabarits actuels, renouvellement fini,
récupération de RKE2 d'un cluster sans Internet, CA des services, entrées
Flux, et `additional-ca` des nœuds Harvester quand le registre y est
fusionné. Cluster simulé (namespaces et sélecteurs respectés)."""

import base64
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_manifests as mf  # noqa: E402
import capi_renew as cr  # noqa: E402
import registries as rg  # noqa: E402

SPEC = importlib.util.spec_from_file_location("harvester_registry_pending", ROOT / "bin" / "harvester-registry.py")
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)

OLD, NEW = "-----OLD CA-----", "-----NEW CA-----"
BOTH = OLD + "\n" + NEW
DECL = {"name": "amorcage", "endpoint": "172.16.2.76", "ca_pem": OLD, "ca_next_pem": NEW}


def b64(d):
    return {k: base64.b64encode(v.encode()).decode() for k, v in d.items()}


class Kube:
    def __init__(self, objs):
        self.objs = objs

    def get(self, kind, ns, name):
        return copy.deepcopy(self.objs.get((kind, ns, name)))

    def list(self, kind, ns=None, selector=None):
        out = [copy.deepcopy(o) for (k, s, _n), o in self.objs.items() if k == kind and (ns is None or s == ns)]
        for cond in (selector or "").split(","):
            if not cond:
                continue
            key, _, val = cond.partition("=")
            out = [o for o in out if key in ((o.get("metadata") or {}).get("labels") or {})
                   and (not val or o["metadata"]["labels"][key] == val)]
        return out


def secret(name, data, labels=None):
    return {"metadata": {"name": name, "namespace": "web",
                         "labels": dict({"harvester-ops.io/generated": "true"}, **(labels or {}))}, "data": b64(data)}


def world(nodes=BOTH, designated="web-registries-r1", mark=None, fetch=None, svc=None, flux=None, setting=None,
          extra_ca=None):
    ann = {cr.A_RENEWAL: mark} if mark else {}
    objs = {
        ("clusters.cluster.x-k8s.io", "web", "web"): {"metadata": {"name": "web", "namespace": "web",
                                                                    "annotations": ann},
                                                       "spec": {"topology": {"classRef": {"name": "cc"}}}},
        (cr.K_CLASS, "web", "cc"): {"metadata": {"name": "cc"}, "spec": {"controlPlane": {"templateRef": {
            "kind": "RKE2ControlPlaneTemplate", "name": "cp"}}}},
        (cr.K_CP_TEMPLATE, "web", "cp"): {"kind": "RKE2ControlPlaneTemplate", "metadata": {"name": "cp"},
                                          "spec": {"template": {"spec": {"files": [{"path": rg.REGISTRIES_FILE,
                                                   "contentFrom": {"secret": {"name": designated}}}]}}}},
        # l'ancien Secret des nœuds (encore lu par les machines d'avant) et le nouveau
        ("secrets", "web", "web-registries"): secret("web-registries", {"ca-amorcage.crt": OLD},
                                                      {rg.used_label("amorcage"): "used"}),
        ("secrets", "web", "web-registries-r1"): secret("web-registries-r1", {"ca-amorcage.crt": nodes},
                                                         {rg.used_label("amorcage"): "used"}),
    }
    if fetch is not None:
        objs[("secrets", "web", "web-rke2-fetch")] = secret("web-rke2-fetch", {"ca.crt": fetch},
                                                            {rg.LABEL: "amorcage"})
    if svc is not None:
        objs[("secrets", "web", "web-registry-amorcage-ca")] = secret("web-registry-amorcage-ca", {"ca.crt": svc},
                                                                      {rg.LABEL: "amorcage"})
    if flux is not None:
        name = mf.name_of("gitops-apps")
        objs[(mf.K_CRS, "web", name)] = {"metadata": {"name": name, "namespace": "web", "labels": {
            mf.L_ITEM: "gitops-apps", mf.L_CLUSTER: "web"}}}
        objs[("configmaps", "web", name)] = {"data": {mf.DATA_KEY: json.dumps(
            {"kind": "OCIRepository", "spec": {"url": "oci://172.16.2.76/flux/apps"}})}}
        objs[("secrets", "web", name)] = {"stringData": {mf.DATA_KEY: json.dumps(
            {"kind": "Secret", "metadata": {"name": "hops-gitops-apps-ca"}, "stringData": {"ca.crt": flux}})}}
    if setting is not None:
        objs[("settings.harvesterhci.io", None, "containerd-registry")] = {"value": json.dumps(setting)}
        objs[("settings.harvesterhci.io", None, "additional-ca")] = {"value": extra_ca or ""}
    return Kube(objs)


MERGED = {"Mirrors": {}, "Configs": {"172.16.2.76": {"Auth": None}}}
MARK = cr.marker_value(cr.marker(1, {}, ["controlPlane"], ["u"], []))


@pytest.mark.parametrize("kw,expect", [
    ({}, []),
    # I-6.1 : renouvellement pas fini, même si le nouveau Secret porte la CA
    ({"mark": MARK}, [("web/web", "renewing")]),
    # le Secret DÉSIGNÉ ne la porte pas (un autre Secret du namespace, si)
    ({"designated": "web-registries"}, [("web/web", "nodes")]),
    ({"nodes": OLD}, [("web/web", "nodes")]),
    ({"fetch": OLD}, [("web/web", "rke2-fetch")]),
    ({"fetch": BOTH}, []),
    ({"svc": OLD}, [("web/web", "service-ca")]),
    ({"flux": OLD}, [("web/web", "flux:apps")]),
    ({"flux": BOTH}, []),
    ({"setting": MERGED, "extra_ca": OLD}, [("harvester", "harvester-nodes")]),
    ({"setting": MERGED, "extra_ca": BOTH}, []),
    ({"setting": {"Configs": {"other": {}}}, "extra_ca": ""}, []),          # registre pas fusionné
    ({"mark": MARK, "fetch": OLD, "nodes": OLD},
     [("web/web", "renewing"), ("web/web", "nodes"), ("web/web", "rke2-fetch")]),
])
def test_ca_pending(kw, expect):
    got = cli.ca_pending(world(**kw), dict(DECL))
    assert [(p["where"], p["reason"]) for p in got] == expect


def test_no_transition_no_pending():
    assert cli.ca_pending(world(nodes=OLD), dict(DECL, ca_next_pem=None)) == []


def test_pending_text():
    assert cli.pending_text([{"where": "web/web", "reason": "nodes"}, {"where": "harvester",
                                                                       "reason": "harvester-nodes"}]) \
        == "web/web (nodes), harvester (harvester-nodes)"
