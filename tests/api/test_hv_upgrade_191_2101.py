"""2.10.1 tâche 7 : mise à jour vers Harvester 1.9.1 (contrôles et fenêtre).

Contrôles propres à une cible v1.9.1 ou plus, jugés sur la version LUE de la
cible (nom de la `Version`, `harvester-release.yaml` d'un ISO), jamais sur un
nom de fichier :

| code | niveau |
|---|---|
| lvm-pre-ga-volumes | bloquant, forçable |
| lvm-striped-volumes | avertissement |
| rancher-vcluster-deprecated | avertissement |
| ingress-nginx-objects | avertissement |
| vm-migration-failure-policy | information |

Point 325 A : case « arrêter les VMs dont la migration échoue » écrite dans
`upgrade-config` par l'action de démarrage, valeur d'origine rendue quand la
mise à jour est finie.

Hypothèses à confirmer au banc (tâche 1, essai 9) : `upgrade-config` porte la
clé `shutdownVMOnMigrationFailure` (booléen, défaut false) au premier niveau
de son JSON ; l'add-on s'appelle `rancher-vcluster` ; une Ingress « nginx » se
reconnaît à `spec.ingressClassName: nginx` ou à une annotation
`nginx.ingress.kubernetes.io/*`.

Mutations rapportées (rapport 2.10.1, tâche 7) : « contrôle sur le nom de
l'ISO » -> `test_route_judges_the_iso_on_its_release` échoue ; « bloquant non
forçable » / « jamais bloquant » -> `test_cli_blocks_pre_ga_lvm_unless_forced`
échoue ; « valeur d'origine jamais rendue » -> `test_cli_policy_written_then_restored` échoue.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import hv_upgrade as hu  # noqa: E402

LVM_NONE = {"lvm": [], "striped": []}
LVM_PRE = {"lvm": ["default/data"], "striped": []}
LVM_STRIPED = {"lvm": ["prod/db"], "striped": ["prod/db"]}
VCLUSTER_ON = {"metadata": {"name": "rancher-vcluster", "namespace": "rancher-vcluster"}, "spec": {"enabled": True}}
VCLUSTER_OFF = {"metadata": {"name": "rancher-vcluster", "namespace": "rancher-vcluster"}, "spec": {"enabled": False}}
ING_CLASS = {"metadata": {"name": "web", "namespace": "apps"}, "spec": {"ingressClassName": "nginx"}}
ING_ANN = {"metadata": {"name": "api", "namespace": "apps",
                        "annotations": {"nginx.ingress.kubernetes.io/rewrite-target": "/"}}, "spec": {}}
ING_TRAEFIK = {"metadata": {"name": "t", "namespace": "apps"}, "spec": {"ingressClassName": "traefik"}}


def keys(rows):
    return {r["key"]: r for r in rows}


@pytest.mark.parametrize("label,current,target,lvm,addons,ings,ingress,want", [
    ("target 1.9.0: nothing", "v1.8.2", "v1.9.0", LVM_PRE, [VCLUSTER_ON], [ING_CLASS], "nginx", set()),
    ("target unreadable: nothing", "v1.9.0", "latest", LVM_PRE, [], [], "nginx", set()),
    ("clean 1.9.0 -> 1.9.1", "v1.9.0", "v1.9.1", LVM_NONE, [], [], "traefik", {"vm-migration-failure-policy"}),
    ("pre-GA LVM", "v1.9.0", "v1.9.1", LVM_PRE, [], [], "traefik",
     {"lvm-pre-ga-volumes", "vm-migration-failure-policy"}),
    ("already GA: no pre-GA check", "v1.9.1", "v1.9.2", LVM_PRE, [], [], "traefik", {"vm-migration-failure-policy"}),
    ("striped volumes", "v1.9.0", "v1.9.1", LVM_STRIPED, [], [], "traefik",
     {"lvm-pre-ga-volumes", "lvm-striped-volumes", "vm-migration-failure-policy"}),
    ("rancher-vcluster on", "v1.9.0", "v1.9.1", LVM_NONE, [VCLUSTER_ON], [], "traefik",
     {"rancher-vcluster-deprecated", "vm-migration-failure-policy"}),
    ("rancher-vcluster off", "v1.9.0", "v1.9.1", LVM_NONE, [VCLUSTER_OFF], [], "traefik",
     {"vm-migration-failure-policy"}),
    ("nginx Ingresses under ingress-nginx", "v1.9.0", "v1.9.1", LVM_NONE, [], [ING_CLASS, ING_ANN, ING_TRAEFIK],
     "nginx", {"ingress-nginx-objects", "vm-migration-failure-policy"}),
    ("nginx Ingresses under Traefik: already migrated", "v1.9.0", "v1.9.1", LVM_NONE, [], [ING_CLASS], "traefik",
     {"vm-migration-failure-policy"}),
    ("dev target", "v1.9.0", "dev", LVM_NONE, [], [], "traefik", {"vm-migration-failure-policy"}),
    ("v1.10.0 target", "v1.9.1", "v1.10.0", LVM_NONE, [VCLUSTER_ON], [], "traefik",
     {"rancher-vcluster-deprecated", "vm-migration-failure-policy"}),
])
def test_target_checks(label, current, target, lvm, addons, ings, ingress, want):
    rows = hu.target_checks(current, target, lvm, addons, ings, ingress)
    assert set(keys(rows)) == want, label


def test_levels_and_texts():
    rows = keys(hu.target_checks("v1.9.0", "v1.9.1", LVM_STRIPED, [VCLUSTER_ON], [ING_CLASS, ING_ANN], "nginx"))
    assert rows["lvm-pre-ga-volumes"]["level"] == "block" and rows["lvm-pre-ga-volumes"]["forcible"] is True
    assert "prod/db" in rows["lvm-pre-ga-volumes"]["text"]
    assert rows["lvm-striped-volumes"]["level"] == "warn"
    assert rows["rancher-vcluster-deprecated"]["level"] == "warn"
    assert rows["ingress-nginx-objects"]["level"] == "warn" and "apps/api" in rows["ingress-nginx-objects"]["text"]
    assert rows["vm-migration-failure-policy"]["level"] == "info"
    assert all(r["ok"] is False for r in rows.values() if r["level"] != "info")


@pytest.mark.parametrize("label,setting,on,value", [
    ("empty value, default used", {"value": "", "default": '{"restoreVM":false}'}, True,
     {"restoreVM": False, "shutdownVMOnMigrationFailure": True}),
    ("value kept", {"value": '{"logReadyTimeout":"5"}', "default": "{}"}, True,
     {"logReadyTimeout": "5", "shutdownVMOnMigrationFailure": True}),
    ("off", {"value": '{"shutdownVMOnMigrationFailure":true}', "default": "{}"}, False,
     {"shutdownVMOnMigrationFailure": False}),
])
def test_migration_policy_value(label, setting, on, value):
    new, orig = hu.migration_policy_value(setting, on)
    assert json.loads(new) == value and orig == setting["value"], label


def test_migration_policy_value_refuses_bad_json():
    with pytest.raises(ValueError, match="upgrade-config"):
        hu.migration_policy_value({"value": "{not json", "default": ""}, True)


# ---------------------------------------------------------------------------
# Outil : blocage forçable, réglage écrit puis rendu
# ---------------------------------------------------------------------------
@pytest.fixture
def cli(monkeypatch):
    import importlib.util
    s = importlib.util.spec_from_file_location("harvester_resources_2101_t7", ROOT / "bin" / "harvester-resources.py")
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    monkeypatch.setattr(mod, "step", lambda *a, **k: None)
    return mod


class K:
    def __init__(self, lvm_pv=False, cfg='{"restoreVM":false}', settle=True):
        self.patches, self.created = [], []
        self.cfg, self.settle = cfg, settle
        self.lvm_pv = lvm_pv

    def get(self, kind, ns, name):
        if name == "server-version":
            return {"value": "v1.9.0"}
        if name == "upgrade-config":
            return {"metadata": {"name": name}, "value": self.cfg, "default": "{}"}
        if kind == hu.K_VERSION:
            return {"metadata": {"name": name}, "spec": {}}
        if kind == hu.K_UPGRADE:
            return {"metadata": {"name": name}, "status": {"conditions": [
                {"type": "Completed", "status": "True" if self.settle else "Unknown"}]}}
        return None

    def list(self, kind, ns=None, selector=None):
        if kind == "persistentvolumes" and self.lvm_pv:
            return [{"metadata": {"name": "pv1"}, "spec": {"csi": {"driver": "lvm.driver.harvesterhci.io"},
                                                          "storageClassName": "local",
                                                          "claimRef": {"namespace": "default", "name": "data"}}}]
        return []

    def patch(self, kind, ns, name, patch):
        self.patches.append((name, patch))
        if name == "upgrade-config":
            self.cfg = patch["value"]

    def create(self, obj):
        self.created.append(obj)
        return dict(obj, metadata={"name": "hvst-upgrade-x"})


def _args(**kw):
    a = dict(action="start", iso=None, version="v1.9.1", version_file=None, no_log=False, skip_single_replica=False,
             timeout=10, force_lvm_pre_ga=False, shutdown_vm_on_migration_failure=False)
    a.update(kw)
    return type("A", (), a)()


@pytest.mark.parametrize("lvm,force,started", [(True, False, False), (True, True, True), (False, False, True)])
def test_cli_blocks_pre_ga_lvm_unless_forced(cli, monkeypatch, lvm, force, started):
    k = K(lvm_pv=lvm)
    monkeypatch.setattr(cli, "kube_from", lambda a: k)
    monkeypatch.setattr(cli, "_follow_upgrade", lambda kube, name, timeout: 0)
    if started:
        cli.cmd_upgrade(_args(force_lvm_pre_ga=force))
        assert len(k.created) == 1
    else:
        with pytest.raises(ValueError, match="lvm-pre-ga-volumes"):
            cli.cmd_upgrade(_args(force_lvm_pre_ga=force))
        assert k.created == []


@pytest.mark.parametrize("settle,restored", [(True, True), (False, False)])
def test_cli_policy_written_then_restored(cli, monkeypatch, settle, restored):
    k = K(settle=settle)
    order = []
    monkeypatch.setattr(cli, "kube_from", lambda a: k)
    monkeypatch.setattr(cli, "_follow_upgrade", lambda kube, name, timeout: order.append(("follow", k.cfg)) or 0)
    cli.cmd_upgrade(_args(shutdown_vm_on_migration_failure=True))
    assert json.loads(order[0][1])["shutdownVMOnMigrationFailure"] is True         # posé avant le suivi
    assert (k.cfg == '{"restoreVM":false}') is restored


def test_cli_without_the_box_never_touches_upgrade_config(cli, monkeypatch):
    k = K()
    monkeypatch.setattr(cli, "kube_from", lambda a: k)
    monkeypatch.setattr(cli, "_follow_upgrade", lambda kube, name, timeout: 0)
    cli.cmd_upgrade(_args())
    assert not [p for p in k.patches if p[0] == "upgrade-config"]


# ---------------------------------------------------------------------------
# Route : contrôles par cible, version lue de l'ISO, drapeaux de démarrage
# ---------------------------------------------------------------------------
def test_route_judges_the_iso_on_its_release(monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT / "web"))
    import app as wapp
    wapp.app.config["TESTING"] = True
    iso = tmp_path / "harvester-v1.9.0-amd64.iso"            # nom trompeur : la release dit v1.9.1
    iso.write_bytes(b"x")
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/kc")
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc: True)
    monkeypatch.setattr(wapp, "_iso_dir", lambda: tmp_path)
    monkeypatch.setattr(wapp, "_iso_release_info", lambda p: {"harvester": "v1.9.1", "min_upgradable": "v1.8.0"})
    monkeypatch.setattr(wapp, "_iso_sha512", lambda n: "")

    def kj(kc, *a, **kw):
        if "server-version" in a:
            return {"value": "v1.9.0"}
        if a[1] == hu.K_VERSION:
            return {"items": [{"metadata": {"name": "v1.9.0"}, "spec": {}}, {"metadata": {"name": "v1.9.1"}, "spec": {}}]}
        if a[1] == "persistentvolumes":
            return {"items": [{"metadata": {"name": "pv1"}, "spec": {"csi": {"driver": "lvm.driver.harvesterhci.io"},
                                                                     "claimRef": {"namespace": "d", "name": "x"}}}]}
        return {"items": []}
    monkeypatch.setattr(wapp, "_kubectl_json", kj)
    d = wapp.app.test_client().get("/api/upgrade/h1").get_json()
    assert "lvm-pre-ga-volumes" in {c["key"] for c in d["isos"][0]["checks"]}
    rows = {v["name"]: {c["key"] for c in v["checks"]} for v in d["versions"]}
    assert rows["v1.9.0"] == set() and "lvm-pre-ga-volumes" in rows["v1.9.1"]


@pytest.mark.parametrize("body,flags", [
    ({}, []),
    ({"shutdown_vm_on_migration_failure": True}, ["--shutdown-vm-on-migration-failure"]),
    ({"force_lvm_pre_ga": True}, ["--force-lvm-pre-ga"]),
])
def test_route_start_passes_the_flags(monkeypatch, body, flags):
    sys.path.insert(0, str(ROOT / "web"))
    import app as wapp
    wapp.app.config["TESTING"] = True
    seen = []
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/kc")
    monkeypatch.setattr(wapp, "_res_cli", lambda cl, kc, label, args, files: (seen.append(args) or "R", None))
    monkeypatch.setattr(wapp, "_res_reply", lambda run, err, **kw: ("{}", 201))
    r = wapp.app.test_client().post("/api/upgrade/h1/do/start", json=dict(body, version="v1.9.1"))
    assert r.status_code == 201
    got = [a for a in seen[0] if a in ("--shutdown-vm-on-migration-failure", "--force-lvm-pre-ga")]
    assert got == flags
