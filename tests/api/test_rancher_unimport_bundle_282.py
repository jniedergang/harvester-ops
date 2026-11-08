"""2.8.2 (décision 247, point 4 du banc 2.8.1) : le retrait de l'import d'un
cluster enfant d'un Rancher externe supprime aussi le Bundle
`fleet-agent-<cluster>` que Rancher 2.14 laisse dans `fleet-default` (vu :
`fleet-agent-harv4-r12a`, propriétaire le Namespace, empreinte d'objectset,
encore là 45 min après le retrait).

Supprimé seulement s'il est l'agent de CE cluster (nom exact, chaque cible
nomme ce cluster) et porte une marque de Rancher ou de Fleet ; dit dans les
étapes ; gardé et dit sinon. Les deux chemins de retrait sont couverts :
`rancher-unimport` (nettoyage du cluster enfant) et la suppression du
cluster (`delete`, puis Défaire, `cleanup=False`). Sans session Rancher,
la règle existante (`rancher-session-needed`) s'applique.

Vérifié par les tests seulement : le Bundle réel (cible, marques) est à
relever au prochain banc.

Mutations jouées (rapport 2.8.2) : B1 cible non vérifiée, B2 marque non
vérifiée, B3 Bundle supprimé avant le retrait du cluster dans Rancher, B4
essai qui supprime, B5 étiquette de la console ignorée.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import capi_stack as cs  # noqa: E402
import rancher_import as ri  # noqa: E402
import test_capi_rancher_import_cli_280 as t0  # noqa: E402

cli = t0.cli
world = t0.world
NAME, NS, CL, H, SERVER = t0.NAME, t0.NS, t0.CL, t0.H, t0.SERVER
BNAME = f"fleet-agent-{NAME}"
BPATH = f"{ri.BUNDLE_API}/fleet-default/{BNAME}"


def bundle(name=BNAME, ns="fleet-default", targets=None, labels=None, annotations=None):
    return {"type": "fleet.cattle.io.bundle",
            "metadata": {"name": name, "namespace": ns,
                         "labels": {"objectset.rio.cattle.io/hash": "0a1b2c3d4e5f"} if labels is None else labels,
                         "annotations": {"objectset.rio.cattle.io/id": "fleet-manage-agent"}
                         if annotations is None else annotations,
                         "ownerReferences": [{"apiVersion": "v1", "kind": "Namespace", "name": "fleet-default"}]},
            "spec": {"targets": [{"clusterName": NAME}] if targets is None else targets}}


class Rancher(t0.Rancher):
    """Faux Rancher 2.14 : le Bundle de l'agent reste après le retrait."""

    def __init__(self, bundles=None, bundle_status=None, bundle_delete=200, **kw):
        super().__init__(**kw)
        self.bundles = dict(bundles or {})
        self.bundle_status, self.bundle_delete = bundle_status, bundle_delete

    def call(self, method, path, body=None):
        if path.startswith(ri.BUNDLE_API + "/"):
            self.calls.append((method, path))
            name = path.rsplit("/", 1)[1]
            if method == "GET":
                if self.bundle_status:
                    return self.bundle_status, {}
                return (200, self.bundles[name]) if name in self.bundles else (404, {"code": "NotFound"})
            if method == "DELETE":
                if self.bundle_delete in (200, 204):
                    self.bundles.pop(name, None)
                return self.bundle_delete, {}
        return super().call(method, path, body)


def imported(rancher_name=NAME, **kw):
    body = ri.cluster_body(rancher_name, H, NS, CL, "bob")
    body["status"] = {"clusterName": t0.CID, "ready": True}
    mgmt = t0.Mgmt({(cs.K_CLUSTER, NS, CL): t0.capi_cluster(
        annotations={ri.A_MARK: f"{SERVER}/{rancher_name}/{t0.CID}"})})
    r = Rancher(existing={rancher_name: body}, **kw)
    return mgmt, r


def steps(capsys):
    return capsys.readouterr().err


DELETE = ["delete", "--kubeconfig", "/x", "--harvester-name", H, "--name", f"{NS}/{CL}"]


@pytest.mark.parametrize("argv", [t0.UNIMPORT, DELETE], ids=["unimport", "delete"])
def test_the_agent_bundle_goes_after_the_cluster(cli, world, capsys, monkeypatch, argv):
    mgmt, r = imported(bundles={BNAME: bundle()})
    monkeypatch.setattr(cli, "remove_cluster", lambda *a, **k: 0)
    assert world["run"](mgmt, r, t0.Child(), argv) == 0
    err = steps(capsys)
    assert BNAME not in r.bundles
    dels = [c for c in r.calls if c[0] == "DELETE"]
    assert dels == [("DELETE", f"{ri.API_V1}/fleet-default/{NAME}"), ("DELETE", BPATH)]       # B3 : après
    assert f"Rancher agent bundle fleet-default/{BNAME} deleted in {SERVER}" in err


@pytest.mark.parametrize("case,b,why", [
    ("other-cluster-target", bundle(targets=[{"clusterName": "harvlab2-autre"}]), "kept-not-rancher"),      # B1
    ("second-target", bundle(targets=[{"clusterName": NAME}, {"clusterName": "harvlab2-autre"}]),
     "kept-not-rancher"),
    ("no-target", bundle(targets=[]), "kept-not-rancher"),
    ("selector-only", bundle(targets=[{"clusterSelector": {}}]), "kept-not-rancher"),
    ("no-mark", bundle(labels={}, annotations={}), "kept-not-rancher"),                                     # B2
    ("console-label", bundle(labels={"objectset.rio.cattle.io/hash": "x", ri.MANAGED: "true"}),
     "kept-not-rancher"),                                                                                     # B5
    ("other-namespace", bundle(ns="fleet-local"), "kept-not-rancher"),
])
def test_a_bundle_that_is_not_exactly_the_agent_is_kept(cli, world, capsys, case, b, why):
    mgmt, r = imported(bundles={BNAME: b})
    assert world["run"](mgmt, r, t0.Child(), t0.UNIMPORT) == 0
    err = steps(capsys)
    assert BNAME in r.bundles and ("DELETE", BPATH) not in r.calls
    assert f"{why}: Bundle fleet-default/{BNAME} kept" in err


@pytest.mark.parametrize("case,kw,needle", [
    ("absent", {}, f"no agent bundle {BNAME} left in fleet-default"),
    ("unreadable", {"bundle_status": 500}, f"agent bundle {BNAME} unreadable (Rancher answered 500), left in place"),
    ("forbidden-read", {"bundle_status": 403}, "(Rancher answered 403), left in place"),
    ("delete-forbidden", {"bundles": {BNAME: bundle()}, "bundle_delete": 403},
     f"rancher-forbidden: Rancher refused to delete the agent bundle {BNAME}, left in place"),
    ("delete-failed", {"bundles": {BNAME: bundle()}, "bundle_delete": 500},
     f"agent bundle {BNAME} not deleted (Rancher answered 500), left in place"),
])
def test_each_bundle_error_is_said_and_the_unimport_still_ends(cli, world, capsys, case, kw, needle):
    """Le cluster est déjà retiré de Rancher : un échec sur le Bundle est dit,
    l'import est retiré quand même (marque enlevée)."""
    mgmt, r = imported(**kw)
    assert world["run"](mgmt, r, t0.Child(), t0.UNIMPORT) == 0
    assert needle in steps(capsys)
    assert ri.A_MARK not in mgmt.objs[(cs.K_CLUSTER, NS, CL)]["metadata"]["annotations"]


def test_rancher_unreachable_on_the_bundle_is_said(cli, world, capsys):
    mgmt, r = imported(bundles={BNAME: bundle()})
    real = r.call

    def call(method, path, body=None):
        if path.startswith(ri.BUNDLE_API):
            raise RuntimeError("rancher-unreachable: timed out")
        return real(method, path, body)
    r.call = call
    assert world["run"](mgmt, r, t0.Child(), t0.UNIMPORT) == 0
    assert f"agent bundle {BNAME} unreadable, left in place: rancher-unreachable" in steps(capsys)


def test_a_long_name_is_not_looked_for(cli, world, capsys):
    """Fleet raccourcit `fleet-agent-<nom>` au-delà de 63 caractères : non
    reconnu exactement, rien n'est lu ni supprimé."""
    long_name = "harvlab2-" + "a" * 50
    assert len(long_name) <= ri.NAME_MAX < len("fleet-agent-" + long_name)
    mgmt, r = imported(rancher_name=long_name)
    assert world["run"](mgmt, r, t0.Child(), t0.UNIMPORT) == 0
    assert f"kept-not-matched: the agent bundle of {long_name} is not looked for" in steps(capsys)
    assert not [c for c in r.calls if c[1].startswith(ri.BUNDLE_API)]


def test_dry_run_reports_the_bundle_without_deleting(cli, world, capsys):
    """B4 : l'essai lit et dit, ne supprime rien."""
    mgmt, r = imported(bundles={BNAME: bundle()})
    assert world["run"](mgmt, r, t0.Child(), t0.UNIMPORT + ["--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["agent_bundle"] == {"name": f"fleet-default/{BNAME}", "present": True, "delete": True}
    assert BNAME in r.bundles and not [c for c in r.calls if c[0] == "DELETE"]


def test_dry_run_without_cleanup(cli, world, capsys, monkeypatch):
    mgmt, r = imported(bundles={BNAME: bundle(labels={}, annotations={})})
    monkeypatch.setattr(cli, "_session_of", lambda kc: world["box"]["session"])
    monkeypatch.setattr(cli, "RANCHER", lambda s: r)
    assert cli.rancher_unimport(mgmt, NS, CL, H, dry=True, cleanup=False) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"rancher": SERVER, "name": NAME, "present": True,
                   "agent_bundle": {"name": f"fleet-default/{BNAME}", "present": True, "delete": False}}
    assert not [c for c in r.calls if c[0] == "DELETE"] and not mgmt.patches


def test_without_session_nothing_is_touched(cli, world, capsys):
    mgmt, r = imported(bundles={BNAME: bundle()})
    world["box"]["session"] = None
    assert world["run"](mgmt, r, t0.Child(), t0.UNIMPORT) == 2
    assert "rancher-session-needed" in steps(capsys)
    assert BNAME in r.bundles and r.calls == []


def test_a_cluster_left_in_rancher_keeps_its_bundle(cli, world, capsys):
    """Rancher refuse le retrait du cluster : le Bundle n'est pas touché."""
    mgmt, r = imported(bundles={BNAME: bundle()}, delete_refused=True)
    assert world["run"](mgmt, r, t0.Child(), t0.UNIMPORT) == 2
    assert BNAME in r.bundles and ("GET", BPATH) not in r.calls


@pytest.mark.parametrize("case,b,name,want", [
    ("exact", bundle(), NAME, True),
    ("other-name", bundle(name="fleet-agent-x"), NAME, False),
    ("prefix-only", bundle(name=BNAME + "-2"), NAME, False),
    ("empty-name", bundle(name="fleet-agent-"), "", False),
    ("fleet-label-mark", bundle(labels={"fleet.cattle.io/bundle-namespace": "fleet-default"}, annotations={}),
     NAME, True),
    ("garbage", None, NAME, False),
])
def test_agent_bundle_ours(case, b, name, want):
    assert ri.agent_bundle_ours(b, name) is want
