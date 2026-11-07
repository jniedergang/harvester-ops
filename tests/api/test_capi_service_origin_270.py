"""2.7.0 : un chart de paquet appliqué à un cluster enfant porte sur son
HelmChartProxy les étiquettes de l'élément (élément, paquet, cluster), la
version et la personne, et l'origine d'un plan (`plan`, `plan-run`) : la
phase Artefacts ne marque `created` que ce qui porte SON exécution, et le
Défaire le retrouve. Mêmes étiquettes que le ConfigMap et le CRS d'un
manifeste (capi_manifests.meta_of, une seule écriture)."""

import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import capi_manifests as mf  # noqa: E402
import capi_services as sv  # noqa: E402
from test_capi_service_registry_260 import SRC, _kube, world  # noqa: E402,F401
from test_capi_services_cli import cli, release  # noqa: E402,F401

RUN = "0123456789ab"


@pytest.mark.parametrize("raw,error", [
    ({"origin": {"plan": "p"}}, "origin"),                          # catalogue : pas d'origine
    ({"source": SRC, "origin": ["plan=p"]}, "origin"),
    ({"source": SRC, "origin": {"plan": 3}}, "origin"),
    ({"source": SRC, "by": "a\nb"}, "by"),
    ({"source": SRC, "by": "x" * 129}, "by"),
    ({"source": SRC, "by": 5}, "by"),
])
def test_normalize_refuses(raw, error):
    with pytest.raises(ValueError, match=error):
        sv.normalize(dict({"cluster": "web/web", "service": "ingress-nginx"}, **raw))


def test_normalize_keeps_origin_and_by():
    s = sv.normalize({"cluster": "web/web", "source": SRC, "origin": {"plan": "p", "run": RUN}, "by": "ju"})
    assert s["origin"] == {"plan": "p", "run": RUN} and s["by"] == "ju"
    assert "origin" not in sv.normalize({"cluster": "web/web", "source": SRC})


def test_manifest_and_crs_share_the_same_marks():
    labels, ann = mf.meta_of("podinfo", "socle", "2026.11", "web", {"plan": "p", "run": RUN}, "ju")
    hcp = sv.manifest(sv.normalize({"cluster": "web/web", "source": SRC, "repo": "oci://r/charts",
                                    "chart": "podinfo"}), None, (labels, ann))
    cm = mf.docs("web", "web", "podinfo", "socle", "2026.11", [], {"plan": "p", "run": RUN}, "ju")[0]
    for k, v in cm["metadata"]["labels"].items():
        assert hcp["metadata"]["labels"][k] == v
    for k, v in cm["metadata"]["annotations"].items():
        assert hcp["metadata"]["annotations"][k] == v
    # le service garde les siennes
    assert hcp["metadata"]["annotations"]["harvester-ops.io/service"] == "registry"
    assert mf.artefact_of(hcp) == {"item": "podinfo", "package": "socle", "version": "2026.11",
                                   "cluster": "web", "by": "ju", "origin": {"plan": "p", "run": RUN}}


def test_a_catalog_service_carries_no_item():
    hcp = sv.manifest(sv.normalize({"cluster": "web/web", "service": "ingress-nginx"}))
    assert mf.artefact_of(hcp) is None
    assert hcp["metadata"]["labels"] == {sv.MANAGED: "true"}


def _deploy(cli, monkeypatch, tmp_path, kube, **body):
    monkeypatch.setattr(cli, "kube_from", lambda a: (kube, None, None))
    monkeypatch.setattr(cli, "_caaph_ready", lambda k: True)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    p = tmp_path / "svc.json"
    p.write_text(json.dumps(dict({"cluster": "web/web", "source": SRC}, **body)))
    return cli.cmd_service_deploy(argparse.Namespace(spec=str(p), timeout=60, harvester_name="harvlab2"))


@pytest.mark.parametrize("origin,rc,run", [
    ({"plan": "socle", "run": RUN}, "EXIT_OK", RUN),
    ({}, "EXIT_OK", None),
    ({"plan": "Bad Name"}, "EXIT_BLOCKED", None),          # contrôlé par capi_cluster.parse_origin
    ({"run": "xyz"}, "EXIT_BLOCKED", None),
])
def test_deploy_writes_the_origin_on_the_hcp(cli, world, monkeypatch, tmp_path, origin, rc, run):
    kube = _kube(cli, on_apply=lambda k: k.objs.__setitem__(
        (cli.sv.K_HRP, "web", "r"), release("web", "podinfo", "web")))
    body = {"by": "ju"}
    if origin:
        body["origin"] = origin
    try:
        got = _deploy(cli, monkeypatch, tmp_path, kube, **body)
    except ValueError:
        got = cli.EXIT_BLOCKED                  # main() rend EXIT_BLOCKED sur ValueError
    assert got == getattr(cli, rc)
    if rc != "EXIT_OK":
        assert kube.applied == []
        return
    hcp = next(d for d in kube.applied if d["kind"] == "HelmChartProxy")
    art = mf.artefact_of(hcp)
    assert art["item"] == "podinfo" and art["package"] == "socle" and art["by"] == "ju"
    assert art["origin"].get("run") == run
