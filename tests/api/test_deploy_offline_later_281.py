"""2.8.1 (banc R12) : cluster enfant sans Internet dans un plan dont la phase
Registre pose le registre d'amorçage et y publie l'élément rke2.

Vu en réel sur harv4 : le contrôle avant lancement refusait le plan
(`offline-needs-no-fallback`, `offline-rke2-missing` bloquants) parce que le
registre n'existe pas encore. Ces deux constats deviennent `offline-later`
(info) quand ce plan les satisfera ; `before()` de la phase Clusters rejoue
`check --many` une fois le registre posé et le paquet publié.

* registre posé par le plan, miroir docker.io, repli coupé : info ;
* sans repli coupé, sans miroir docker.io, ou registre déjà là : bloquant ;
* rke2 publié par le plan dans un registre du cluster, même version : info ;
* autre version, autre registre, paquet sans rke2 : bloquant.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import registries as rg  # noqa: E402
import test_deploy_registry_260 as tr  # noqa: E402

env = tr.env
world = tr.world
capi = tr.capi
reg = tr.reg

REF = "harvlab-apps/harvlab-apps"


def rke2_package(state, name="rke2", version="1.34.11-rke2r1", rke2="v1.34.11+rke2r1"):
    d = Path(state) / "artefacts.d" / f"{name}-{version}"
    d.mkdir(parents=True, exist_ok=True)
    items = [{"name": "rke2", "kind": "rke2", "version": rke2}] if rke2 else []
    (d / "artefacts.yaml").write_text(json.dumps({"name": name, "version": version, "items": items}))
    (d / "trust.json").write_text(json.dumps({"state": "valid"}))
    (d / "blob").write_bytes(b"x" * 1024)


def offline_plan(no_fallback=True, mirrors=("docker.io", "ghcr.io"), publish_reg="amorcage",
                 cluster_reg="amorcage", rke2_version="1.34.11-rke2r1"):
    body = tr.plan()
    ph = body["phases"]["registry"]
    ph["registries"][0]["mirrors"] = list(mirrors)
    ph["registries"].append({"name": "autre", "mode": "bootstrap", "address": "172.16.2.77", "size_gib": 8,
                             "storage_class": "harv-rep1", "mirrors": ["docker.io"]})
    ph["publish"].append({"package": "rke2", "version": rke2_version, "registry": publish_reg})
    r = {"registry": cluster_reg, "mirrors": ["docker.io"]}
    if no_fallback:
        r["no_fallback"] = True
    body["phases"]["clusters"]["items"][0]["registries"] = [r]
    return body


def run_check(w, body, codes):
    for code in codes:
        w["capi"].fleet.append({"code": code, "level": "block", "facts": {"version": "v1.34.11"}, "cluster": REF})
    tr.seed(w, body)
    out = tr.check(w)
    return {(f["code"], f["level"]) for f in out["findings"] if f["phase"] == "clusters"}, out


@pytest.mark.parametrize("case,body_kw,code,later", [
    ("registry-of-the-plan", {}, "offline-needs-no-fallback", True),
    ("fallback-kept", {"no_fallback": False}, "offline-needs-no-fallback", False),
    ("no-docker-mirror", {"mirrors": ("ghcr.io",)}, "offline-needs-no-fallback", False),
    ("rke2-published-by-the-plan", {}, "offline-rke2-missing", True),
    ("rke2-mismatch-published-by-the-plan", {}, "offline-rke2-version-mismatch", True),
    ("rke2-to-another-registry", {"publish_reg": "autre"}, "offline-rke2-missing", False),
    ("rke2-other-version", {"rke2_version": "1.33.9-rke2r1"}, "offline-rke2-missing", False),
])
def test_offline_findings_satisfied_by_the_plan_are_later(reg, case, body_kw, code, later):
    rke2_package(reg["state"])
    rke2_package(reg["state"], version="1.33.9-rke2r1", rke2="v1.33.9+rke2r1")
    got, out = run_check(reg, offline_plan(**body_kw), [code])
    if later:
        assert ("offline-later", "info") in got, out["findings"]
        assert (code, "blocking") not in got
        assert not out["blocking"], out["findings"]
    else:
        assert (code, "blocking") in got, out["findings"]
        assert out["blocking"]


def test_a_package_without_rke2_does_not_satisfy(reg):
    rke2_package(reg["state"], rke2=None)
    got, out = run_check(reg, offline_plan(), ["offline-rke2-missing"])
    assert ("offline-rke2-missing", "blocking") in got and out["blocking"]


def test_a_registry_already_there_is_judged_by_the_tool(reg):
    """Registre présent (repris) : sa déclaration est lue par l'outil, le
    constat de l'outil reste bloquant (rien n'est « plus tard »)."""
    rke2_package(reg["state"])
    rg.save(reg["state"], "amorcage", {"kind": "bootstrap", "endpoint": "172.16.2.76", "usage": ["images"],
                                       "clusters": ["harvlab"], "cluster": "harvlab", "address": "172.16.2.76",
                                       "size_gib": 8, "storage_class": "harv-rep1"})
    reg["reg"].present["amorcage"] = {"origin": {"plan": "site", "run": "aaaaaaaaaaaa"}, "ready": True}
    got, out = run_check(reg, offline_plan(), ["offline-needs-no-fallback"])
    assert ("offline-needs-no-fallback", "blocking") in got and out["blocking"]


def test_the_two_findings_together_are_both_later(reg):
    rke2_package(reg["state"])
    got, out = run_check(reg, offline_plan(), ["offline-needs-no-fallback", "offline-rke2-missing"])
    later = [f for f in out["findings"] if f["code"] == "offline-later"]
    assert len(later) == 2 and not out["blocking"], out["findings"]
