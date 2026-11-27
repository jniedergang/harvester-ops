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


class KU(K):
    """Comme K, avec l'Upgrade créée gardée (annotations) et une API qui peut
    se taire."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.upgrade, self.silent = None, False

    def get(self, kind, ns, name):
        if self.silent:
            from kube import KubeError
            raise KubeError("Unable to connect to the server")
        if kind == hu.K_UPGRADE:
            if self.upgrade is None:
                return None
            return json.loads(json.dumps(dict(self.upgrade, status={"conditions": [
                {"type": "Completed", "status": "True" if self.settle else "Unknown"}]})))
        return super().get(kind, ns, name)

    def create(self, obj):
        self.created.append(obj)
        self.upgrade = json.loads(json.dumps(dict(obj, metadata=dict(obj.get("metadata") or {},
                                                                      name="hvst-upgrade-x"))))
        return self.upgrade

    def patch(self, kind, ns, name, patch):
        if kind == hu.K_UPGRADE:
            self.patches.append((name, patch))
            ann = self.upgrade["metadata"].setdefault("annotations", {})
            for k, v in ((patch.get("metadata") or {}).get("annotations") or {}).items():
                ann.pop(k, None) if v is None else ann.__setitem__(k, v)
            labels = self.upgrade["metadata"].setdefault("labels", {})
            labels.update((patch.get("metadata") or {}).get("labels") or {})
            return
        super().patch(kind, ns, name, patch)


def _args(**kw):
    a = dict(action="start", iso=None, version="v1.9.1", version_file=None, no_log=False, skip_single_replica=False,
             timeout=10, force_lvm_pre_ga=False, shutdown_vm_on_migration_failure=None, name=None)
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
    k = KU(settle=settle)
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


# Relecture I-3 : le service de remplacement de la console (ingress-expose pour
# CAPHV v0.10.1) est dit avant toute mise à jour, quelle que soit la cible :
# Harvester le supprime au redémarrage de son contrôleur rancher quand Traefik
# est là (register.go, v1.9.0 et v1.9.1), il faut le reposer après (Réparer).
@pytest.mark.parametrize("label,current,target,shim,present", [
    ("1.9.1 target with the console service", "v1.9.0", "v1.9.1", True, True),
    ("1.9.0 target with the console service", "v1.8.2", "v1.9.0", True, True),
    ("unreadable target with the console service", "v1.9.0", "latest", True, True),
    ("no console service", "v1.9.0", "v1.9.1", False, False),
])
def test_console_ingress_expose_present(label, current, target, shim, present):
    rows = keys(hu.target_checks(current, target, LVM_NONE, [], [], "traefik", console_shim=shim))
    assert ("console-ingress-expose-present" in rows) is present, label
    if present:
        r = rows["console-ingress-expose-present"]
        assert r["level"] == "warn" and r["forcible"] is False and r["ok"] is False


# Relecture M-2 : les Ingress de Harvester et de Rancher (migrées par la mise à
# jour elle-même) ne comptent pas dans l'avertissement ingress-nginx.
def test_nginx_warning_skips_harvester_and_rancher_ingresses():
    own = [{"metadata": {"name": "rancher-expose", "namespace": "cattle-system"},
            "spec": {"ingressClassName": "nginx"}},
           {"metadata": {"name": "ui", "namespace": "longhorn-system"}, "spec": {"ingressClassName": "nginx"}},
           {"metadata": {"name": "x", "namespace": "harvester-system"}, "spec": {"ingressClassName": "nginx"}}]
    assert "ingress-nginx-objects" not in keys(hu.target_checks("v1.9.0", "v1.9.1", LVM_NONE, [], own, "nginx"))
    rows = keys(hu.target_checks("v1.9.0", "v1.9.1", LVM_NONE, [], own + [ING_CLASS], "nginx"))
    assert rows["ingress-nginx-objects"]["text"] == "apps/web"


def test_route_reads_the_console_shim():
    src = (ROOT / "web" / "app.py").read_text()
    body = src[src.index("def api_upgrade(cluster):"):]
    body = body[:body.index("\n@app.route")]
    assert "console_shim=" in body and "caphv-ingress-expose" in body


# ---------------------------------------------------------------------------
# Relecture I-2 : la valeur d'origine de `upgrade-config` vit sur l'Upgrade
# créée (annotation), pas dans le processus ; `follow` et `dismiss` la rendent
# quand la mise à jour est finie (annulation, délai, API muette, console
# redémarrée). Relecture I-4 : case décochée sur une valeur `true` = `false`.
# ---------------------------------------------------------------------------
ORIG = '{"restoreVM":false}'


def _start(cli, monkeypatch, k, follow, **kw):
    monkeypatch.setattr(cli, "kube_from", lambda a: k)
    monkeypatch.setattr(cli, "_follow_upgrade", follow)
    return cli.cmd_upgrade(_args(**kw))


def test_cli_keeps_the_original_value_on_the_upgrade(cli, monkeypatch):
    k = KU(settle=False)
    _start(cli, monkeypatch, k, lambda kube, name, timeout: 1, shutdown_vm_on_migration_failure=True)
    assert k.upgrade["metadata"]["annotations"][hu.A_CONFIG_BEFORE] == ORIG
    assert json.loads(k.cfg)["shutdownVMOnMigrationFailure"] is True        # laissée : la mise à jour tourne


@pytest.mark.parametrize("label,ending", [
    ("action cancelled (SIGTERM)", "cancel"),
    ("6 h timeout", "timeout"),
    ("API silent for 45 min", "silent"),
    ("console restarted", "restart"),
])
@pytest.mark.parametrize("later", ["follow", "dismiss"])
def test_follow_or_dismiss_sets_the_value_back(cli, monkeypatch, label, ending, later):
    k = KU(settle=False)

    def follow(kube, name, timeout):
        if ending == "cancel":
            raise cli.Cancelled("signal 15")
        if ending == "silent":
            k.silent = True
        if ending == "restart":
            raise SystemExit(143)
        return 1
    try:
        rc = _start(cli, monkeypatch, k, follow, shutdown_vm_on_migration_failure=True)
    except (cli.Cancelled, SystemExit):
        rc = None
    if ending in ("timeout", "silent"):
        assert rc == 1, label                                   # le code de retour survit au finally
    assert json.loads(k.cfg)["shutdownVMOnMigrationFailure"] is True, label
    k.silent, k.settle = False, True                            # la mise à jour finit, l'API revient
    monkeypatch.setattr(cli, "_follow_upgrade", lambda kube, name, timeout: 0)
    cli.cmd_upgrade(_args(action=later, name="hvst-upgrade-x"))
    assert k.cfg == ORIG, (label, later)
    assert hu.A_CONFIG_BEFORE not in k.upgrade["metadata"].get("annotations", {})


def test_follow_of_a_running_upgrade_leaves_the_value(cli, monkeypatch):
    k = KU(settle=False)
    _start(cli, monkeypatch, k, lambda kube, name, timeout: 1, shutdown_vm_on_migration_failure=True)
    cli.cmd_upgrade(_args(action="follow", name="hvst-upgrade-x"))
    assert json.loads(k.cfg)["shutdownVMOnMigrationFailure"] is True
    assert k.upgrade["metadata"]["annotations"][hu.A_CONFIG_BEFORE] == ORIG


def test_unchecked_box_on_a_true_value_writes_false_then_sets_it_back(cli, monkeypatch):
    k = KU(cfg='{"shutdownVMOnMigrationFailure":true}')
    seen = []
    _start(cli, monkeypatch, k, lambda kube, name, timeout: seen.append(k.cfg) or 0,
           shutdown_vm_on_migration_failure=False)
    assert json.loads(seen[0])["shutdownVMOnMigrationFailure"] is False
    assert k.cfg == '{"shutdownVMOnMigrationFailure":true}'


@pytest.mark.parametrize("body,flags", [
    ({"shutdown_vm_on_migration_failure": False}, ["--no-shutdown-vm-on-migration-failure"]),
])
def test_route_start_passes_the_off_flag(monkeypatch, body, flags):
    sys.path.insert(0, str(ROOT / "web"))
    import app as wapp
    wapp.app.config["TESTING"] = True
    seen = []
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/kc")
    monkeypatch.setattr(wapp, "_res_cli", lambda cl, kc, label, args, files: (seen.append(args) or "R", None))
    monkeypatch.setattr(wapp, "_res_reply", lambda run, err, **kw: ("{}", 201))
    wapp.app.test_client().post("/api/upgrade/h1/do/start", json=dict(body, version="v1.9.1"))
    assert [a for a in seen[0] if "migration-failure" in a] == flags


def test_api_says_a_value_left_to_set_back(monkeypatch):
    sys.path.insert(0, str(ROOT / "web"))
    import app as wapp
    wapp.app.config["TESTING"] = True
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/kc")
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc: True)
    monkeypatch.setattr(wapp, "_iso_dir", lambda: Path("/nonexistent"))
    done = {"metadata": {"name": "hvst-upgrade-x", "labels": {hu.L_STATE: "Succeeded", hu.L_LATEST: "true"},
                         "annotations": {hu.A_CONFIG_BEFORE: ""}}}

    def kj(kc, *a, **kw):
        if "server-version" in a:
            return {"value": "v1.9.1"}
        if a[1] == hu.K_UPGRADE:
            return {"items": [done]}
        return {"items": []}
    monkeypatch.setattr(wapp, "_kubectl_json", kj)
    d = wapp.app.test_client().get("/api/upgrade/h1").get_json()
    assert d["migration_policy_pending"] == "hvst-upgrade-x"


# ---------------------------------------------------------------------------
# Relecture finale I-1 : une Upgrade supprimée (Arrêter dans la console, ou
# suppression depuis Harvester) ne doit pas laisser la valeur de la mise à
# jour dans `upgrade-config`. L'action de démarrage rend la valeur qu'elle a
# gardée en mémoire ; l'action Arrêter rend celle de l'annotation AVANT de
# supprimer l'objet (l'annotation part avec lui).
# ---------------------------------------------------------------------------
class KD(KU):
    """KU avec suppression et liste des Upgrades."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.deleted = []
        self.finished = []                  # Upgrades finies déjà là (m2)

    def delete(self, kind, ns, name):
        self.deleted.append((kind, name, self.cfg))
        if kind == hu.K_UPGRADE and self.upgrade and self.upgrade["metadata"]["name"] == name:
            self.upgrade = None

    def list(self, kind, ns=None, selector=None):
        if kind == hu.K_UPGRADE:
            return json.loads(json.dumps(self.finished))
        return super().list(kind, ns, selector)

    def get(self, kind, ns, name):
        if kind == hu.K_UPGRADE and not self.silent:
            for u in self.finished:
                if u["metadata"]["name"] == name:
                    return json.loads(json.dumps(u))
        return super().get(kind, ns, name)

    def patch(self, kind, ns, name, patch):
        if kind == hu.K_UPGRADE:
            for u in self.finished:
                if u["metadata"]["name"] == name:
                    self.patches.append((name, patch))
                    ann = u["metadata"].setdefault("annotations", {})
                    for key, v in ((patch.get("metadata") or {}).get("annotations") or {}).items():
                        ann.pop(key, None) if v is None else ann.__setitem__(key, v)
                    return
        super().patch(kind, ns, name, patch)


def test_abort_during_start_sets_the_value_back(cli, monkeypatch):
    k = KD(settle=False)

    def follow(kube, name, timeout):
        cli.cmd_upgrade(_args(action="abort", name=name))          # Arrêter depuis la fenêtre
        return 1                                                   # « the upgrade object disappeared »
    _start(cli, monkeypatch, k, follow, shutdown_vm_on_migration_failure=True)
    assert k.upgrade is None
    assert k.cfg == ORIG


def test_abort_from_a_separate_action_sets_the_value_back_before_deleting(cli, monkeypatch):
    k = KD(settle=False)

    def follow(kube, name, timeout):
        raise SystemExit(143)                                      # console redémarrée : plus de processus
    with pytest.raises(SystemExit):
        _start(cli, monkeypatch, k, follow, shutdown_vm_on_migration_failure=True)
    assert json.loads(k.cfg)["shutdownVMOnMigrationFailure"] is True
    cli.cmd_upgrade(_args(action="abort", name="hvst-upgrade-x"))
    assert k.cfg == ORIG
    assert k.deleted and k.deleted[0][2] == ORIG                   # rendue AVANT la suppression


def test_upgrade_deleted_in_harvester_during_start_sets_the_value_back(cli, monkeypatch):
    k = KD(settle=False)

    def follow(kube, name, timeout):
        k.upgrade = None                                           # supprimée depuis l'interface de Harvester
        return 1
    _start(cli, monkeypatch, k, follow, shutdown_vm_on_migration_failure=True)
    assert k.cfg == ORIG


def test_abort_without_kept_value_never_touches_upgrade_config(cli, monkeypatch):
    k = KD(settle=False)
    k.upgrade = {"metadata": {"name": "hvst-upgrade-x"}}
    monkeypatch.setattr(cli, "kube_from", lambda a: k)
    cli.cmd_upgrade(_args(action="abort", name="hvst-upgrade-x"))
    assert not [p for p in k.patches if p[0] == "upgrade-config"]
    assert k.upgrade is None


# Relecture finale m2 : une Upgrade finie qui garde encore une valeur à rendre
# est réglée AVANT de lire la valeur d'origine de la nouvelle mise à jour ;
# sinon la valeur laissée deviendrait « l'origine ».
@pytest.mark.parametrize("box", [True, None])
def test_start_settles_a_value_left_by_a_finished_upgrade_first(cli, monkeypatch, box):
    left = '{"restoreVM":false,"shutdownVMOnMigrationFailure":true}'
    k = KD(cfg=left)
    k.finished = [{"metadata": {"name": "hvst-upgrade-old", "labels": {hu.L_STATE: "Succeeded"},
                                "annotations": {hu.A_CONFIG_BEFORE: ORIG}},
                   "status": {"conditions": [{"type": "Completed", "status": "True"}]}}]
    seen = []
    _start(cli, monkeypatch, k, lambda kube, name, timeout: seen.append(k.cfg) or 0,
           shutdown_vm_on_migration_failure=box)
    assert hu.A_CONFIG_BEFORE not in k.finished[0]["metadata"]["annotations"]
    if box:
        assert k.created[-1]["metadata"]["annotations"][hu.A_CONFIG_BEFORE] == ORIG   # la vraie origine
        assert json.loads(seen[0])["shutdownVMOnMigrationFailure"] is True
    assert k.cfg == ORIG
