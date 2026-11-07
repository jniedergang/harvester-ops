"""2.8.0 : corrections de la revue indépendante des tâches 1 à 4.

* I-1 : un reste de Rancher n'est retiré que s'il porte une marque de
  Rancher ; `crb-monitoring` d'un client reste (`kept-not-rancher`) ;
* I-2 : supprimer un cluster importé ne lit rien dans le cluster enfant
  (cassé, il reste supprimable) ; `--forget-rancher` oublie l'import d'un
  Rancher disparu, geste d'administrateur dans la console ;
* I-3 : retrait d'une entrée Flux, la `Kustomization` d'abord et attendue,
  puis la source, puis le compte et ses liaisons ;
* I-4 : Fleet réservé à l'administrateur (aucun compte confiné) ;
* I-5 : nom Fleet propre au cluster enfant, propriété jugée avant toute
  écriture d'un Secret.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "tests" / "api"))

import capi_stack as cs  # noqa: E402
import child_gitops as cg  # noqa: E402
import rancher_import as ri  # noqa: E402
import test_capi_gitops_cli_280 as tg  # noqa: E402
import test_capi_rancher_import_cli_280 as tr  # noqa: E402
from kube import KubeError  # noqa: E402

cli = tr.cli
world = tr.world


# --- I-1 ---------------------------------------------------------------------
@pytest.mark.parametrize("obj,kept", [
    ({"kind": "ClusterRoleBinding", "metadata": {"name": "crb-monitoring"}}, True),
    ({"kind": "ClusterRole", "metadata": {"name": "create-ns", "labels": {"app": "other-tool"}}}, True),
    ({"kind": "Namespace", "metadata": {"name": "local"}}, True),
    ({"kind": "ClusterRoleBinding", "metadata": {"name": "crb-x", "labels": {"cattle.io/creator": "norman"}}}, False),
    ({"kind": "ClusterRole", "metadata": {"name": "p-ab-namespaces-edit",
                                          "annotations": {"authz.cluster.cattle.io/clusterrole-owner": "p"}}}, False),
    ({"kind": "ClusterRoleBinding", "metadata": {"name": "globaladmin-u", "ownerReferences": [
        {"apiVersion": "management.cattle.io/v3", "kind": "GlobalRoleBinding", "name": "g"}]}}, False),
    ({"kind": "ClusterRole", "metadata": {"name": "cattle-admin"}}, False),          # manifeste d'enregistrement
    ({"kind": "CustomResourceDefinition", "metadata": {"name": "settings.management.cattle.io"}}, False),
])
def test_leftovers_need_a_mark_of_rancher(obj, kept):
    unmarked = []
    got = ri.leftovers([obj], unmarked)
    assert (got == []) is kept
    assert bool(unmarked) is (kept and obj["kind"] != "CustomResourceDefinition")


def test_unimport_keeps_a_customer_binding_named_like_rancher(cli, world, capsys):
    mgmt, r = tr.imported_world()
    child = tr.Child(tr.LEFT + [tr._o("ClusterRoleBinding", "crb-monitoring", labels={})])
    assert world["run"](mgmt, r, child, tr.UNIMPORT) == 0
    assert "crb-monitoring" not in child.deleted and "crb-monitoring" not in child.dry
    assert "kept-not-rancher: ClusterRoleBinding crb-monitoring kept" in capsys.readouterr().err


# --- I-2 ---------------------------------------------------------------------
class DeadChild(tr.Child):
    def list(self, *a, **k):
        raise KubeError("Unable to connect to the server: dial tcp 172.16.2.200:6443: no route to host")


DELETE = ["delete", "--kubeconfig", "/x", "--harvester-name", tr.H, "--name", f"{tr.NS}/{tr.CL}"]


def test_delete_of_an_imported_cluster_reads_nothing_in_the_child(cli, world, capsys, monkeypatch):
    mgmt, r = tr.imported_world()
    removed = []
    monkeypatch.setattr(cli, "remove_cluster", lambda *a, **k: removed.append(a[1:3]) or 0)
    assert world["run"](mgmt, r, DeadChild(), DELETE) == 0
    assert removed == [(tr.NS, tr.CL)] and tr.NAME not in r.prov
    assert ri.A_MARK not in mgmt.objs[(cs.K_CLUSTER, tr.NS, tr.CL)]["metadata"]["annotations"]
    assert "nothing removed inside it" in capsys.readouterr().err


def test_delete_of_a_cluster_imported_by_hand_after_a_plan_is_not_refused(cli, world, capsys, monkeypatch):
    """m-2 : la marque d'exécution n'est comparée qu'au cluster."""
    mgmt, r = tr.imported_world(run=None)
    mgmt.objs[(cs.K_CLUSTER, tr.NS, tr.CL)]["metadata"]["annotations"]["harvester-ops.io/plan-run"] = "0123456789ab"
    monkeypatch.setattr(cli, "remove_cluster", lambda *a, **k: 0)
    assert world["run"](mgmt, r, DeadChild(), DELETE + ["--expect-run", "0123456789ab"]) == 0


@pytest.mark.parametrize("case", ["no-session", "unreachable"])
def test_delete_without_rancher_is_refused_with_the_escape_named(cli, world, capsys, monkeypatch, case):
    mgmt, r = tr.imported_world()
    removed = []
    monkeypatch.setattr(cli, "remove_cluster", lambda *a, **k: removed.append(1) or 0)
    if case == "no-session":
        world["box"]["session"] = None
    else:
        def down(*a, **k):
            raise RuntimeError("rancher-unreachable: timed out")
        r.call = down
    assert world["run"](mgmt, r, DeadChild(), DELETE) in (1, 2)
    assert removed == [] and "--forget-rancher" in capsys.readouterr().err


def test_forget_rancher_drops_the_mark_without_rancher(cli, world, capsys, monkeypatch):
    mgmt, r = tr.imported_world()
    world["box"]["session"] = None
    removed = []
    monkeypatch.setattr(cli, "remove_cluster", lambda *a, **k: removed.append(1) or 0)
    assert world["run"](mgmt, r, DeadChild(), DELETE + ["--forget-rancher"]) == 0
    assert removed == [1] and r.calls == []
    assert ri.A_MARK not in mgmt.objs[(cs.K_CLUSTER, tr.NS, tr.CL)]["metadata"]["annotations"]
    assert "rancher-forgotten" in capsys.readouterr().err


# --- I-3 ---------------------------------------------------------------------
class FluxChild:
    """Cluster enfant qui consigne les suppressions ; la Kustomization reste
    `polls` lectures après sa suppression (finaliseur de Flux)."""

    def __init__(self, polls=2):
        self.deleted, self.polls, self.gone = [], polls, set()

    def delete(self, res, ns, name, **kw):
        self.deleted.append(res.split(".")[0])
        self.gone.add(res)

    def get(self, res, ns, name):
        if res.startswith("Kustomization") and res in self.gone:
            self.polls -= 1
            return None if self.polls < 0 else {"metadata": {"name": name}}
        return None


def todo():
    import capi_manifests as mf
    e = cg.normalize(tg.entry())
    objs, secs = cg.flux_objects(e, "oci://r/x", {"pull": {"host": "r", "username": "u", "password": "p"}})
    refs = mf.delete_order([mf.ref_of(o) for o in objs + secs])
    return [(mf.resource_of(r), r, f"{r[1]} {r[3]}") for r in refs]


def test_flux_removal_deletes_the_kustomization_first_and_waits(cli):
    child = FluxChild(polls=2)
    n = cli._prune_gitops(child, todo(), "remove", sleep=lambda s: None)
    assert n == len(todo())
    assert child.deleted[0] == "Kustomization" and child.deleted[1] == "OCIRepository"
    assert set(child.deleted[2:]) == {"ServiceAccount", "RoleBinding", "Secret"}
    assert child.polls < 0                                 # attendue jusqu'à sa disparition


def test_flux_removal_keeps_the_account_when_the_kustomization_stays(cli):
    child = FluxChild(polls=10 ** 6)
    clock = iter(range(0, 10 ** 6, 100))
    with pytest.raises(TimeoutError):
        cli._prune_gitops(child, todo(), "remove", timeout=300, now=lambda: next(clock), sleep=lambda s: None)
    assert child.deleted == ["Kustomization"]


# --- I-4 ---------------------------------------------------------------------
@pytest.mark.parametrize("role,privileges,codes", [
    ("operator", "namespace", ["gitops-fleet-admin-only"]),
    ("operator", "cluster", ["gitops-privileges-admin-only", "gitops-fleet-admin-only"]),
    ("admin", "namespace", ["gitops-fleet-not-confined"]),
    ("admin", "cluster", []),
])
def test_fleet_is_reserved_to_the_administrator(role, privileges, codes):
    e = cg.normalize(dict(tg.entry(source={"type": "git", "url": "https://h/r.git"}), privileges=privileges))
    got = cg.check(e, {"tool": "fleet", "role": role, "published": True})
    assert [f["code"] for f in got] == codes
    assert all(f["level"] == "warn" for f in got if f["code"] == "gitops-fleet-not-confined")


# --- I-5 ---------------------------------------------------------------------
def test_fleet_names_are_per_child_cluster():
    e = {"name": "apps"}
    a, b = cg.fleet_name("harvlab", "web", "web", e), cg.fleet_name("harvlab", "api", "api", e)
    assert (a, b) == ("hops-harvlab-web-apps", "hops-harvlab-api-apps")
    assert cg.fleet_name("harvlab", "equipe", "web", e) == "hops-harvlab-equipe-web-apps"
    long_a = cg.fleet_name("harvlab", "n" * 30, "c" * 30, e)
    long_b = cg.fleet_name("harvlab", "n" * 30, "d" * 30, e)
    assert len(long_a) == 63 and long_a != long_b


def test_the_registration_manifest_file_is_private_while_applied(cli, world):
    """Revue 1-4 (m-10) : fichier 0600 pendant l'application, effacé après."""
    import os
    import stat
    modes = []

    class Probe(tr.Child):
        def run(self, *args, **kw):
            modes.append(stat.S_IMODE(os.stat(args[2]).st_mode))
            return super().run(*args, **kw)
    mgmt = tr.Mgmt({(cs.K_CLUSTER, tr.NS, tr.CL): tr.capi_cluster()})
    assert world["run"](mgmt, tr.Rancher(), Probe(), tr.IMPORT) == 0
    assert modes == [0o600] and list(world["wd"].iterdir()) == []
